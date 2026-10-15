import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, time, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
)

from .config import Settings
from .domain import Draft, Prediction, timestamp, utcnow
from .fonbet import FonbetPrematch, valid_reference
from .leagues import activity_snapshot, calendar_events
from .market_status import MARKET_ISSUES
from .model import History, Model
from .presentation import MOSCOW, caption, market_snapshot, render_card, result_text
from .providers import (
    HTTP,
    OddsFeed,
    OddsPapi,
    OpenDota,
    Polymarket,
    Steam,
    eligible_analysis,
    eligible_draft,
    match_context,
    merge_live_drafts,
    normalized,
    patch_for_time,
)
from .storage import Store
from .timing import draft_timing, separate_timing
from .tournaments import Liquipedia, enrich_markets, tournament_notice_window

log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings: Settings, store: Store, bot: Bot | None = None):
        self.settings, self.store, self.bot = settings, store, bot
        if self.store.get("tournament_alert_window") != "calendar_month":
            self.store.rearm_distant_tournament_notices(tournament_notice_window()[1])
            self.store.set("tournament_alert_window", "calendar_month")
        if not self.store.get("destinations_migrated", False):
            legacy = self.store.get("chat_id", settings.telegram_chat_id)
            if legacy:
                self.store.destination(
                    legacy,
                    self.store.get("chat_title") or str(legacy),
                    self.store.get("chat_type", "group"),
                    self.store.get("topic_id", settings.telegram_topic_id),
                )
            self.store.set("destinations_migrated", True)
        self.http = HTTP(settings.proxy_url or None)
        self.odota = OpenDota(self.http, settings.opendota_api_key)
        self.poly = Polymarket(self.http)
        self.liquipedia = Liquipedia(self.http)
        self.tournament_task = None
        self.steam = Steam(self.http, settings.steam_api_key) if settings.steam_api_key else None
        self.live_cache = None
        self.live_lock = asyncio.Lock()
        self.feed = (
            OddsFeed(self.http, settings.odds_feed_url, settings.odds_feed_api_key)
            if settings.odds_feed_url
            else None
        )
        self.odds = (
            OddsPapi(self.http, settings.oddspapi_api_key, settings.oddspapi_base_url)
            if settings.oddspapi_api_key
            else None
        )
        self.fonbet = (
            FonbetPrematch(
                self.odds,
                store,
                settings.oddspapi_daily_request_limit,
                settings.oddspapi_prefetch_seconds,
            )
            if self.odds
            else None
        )
        self.model = Model(settings.model_path)
        self.shadow_model = (
            Model(settings.shadow_model_path)
            if settings.shadow_model_path and settings.shadow_model_path != settings.model_path
            else None
        )
        self.heroes: dict[int, dict] = {}
        self.player_names: dict[int, str] = {}
        self.patches = self.store.get("patches", [])
        self.live_state = None
        self.shadow_live_state = None
        self.tick_count = 0
        self.results_recent_checked_at = None
        self.tick_lock = asyncio.Lock()
        self.activity_lock = asyncio.Lock()
        self.calendar = calendar_events()
        self.update_tournament_calendar()
        for event in self.calendar:
            self.store.league(int(event["league_id"]), event["name"])
        self.stop = asyncio.Event()
        self.assets = settings.data_dir / "heroes"
        self.assets.mkdir(parents=True, exist_ok=True)

    @property
    def chat_id(self):
        targets = self.destinations
        return targets[0]["chat_id"] if targets else None

    @property
    def destinations(self):
        return self.store.destinations(True)

    def destination_enabled(self, chat_id):
        target = next((t for t in self.store.destinations() if t["chat_id"] == chat_id), None)
        return bool(target["enabled"]) if target else True

    @property
    def topic_id(self):
        targets = self.destinations
        return targets[0]["topic_id"] if targets else None

    @property
    def paused(self):
        return self.store.get("paused", False)

    @property
    def automatic(self):
        return self.store.get("auto_publish", self.settings.auto_publish)

    @property
    def min_probability(self):
        return self.store.get("min_favorite_probability", self.settings.min_favorite_probability)

    @property
    def publish_all_maps(self):
        return self.store.get("publish_all_maps", self.settings.publish_all_maps)

    @property
    def allow_delayed_analysis(self):
        return self.store.get("allow_delayed_analysis", self.settings.allow_delayed_analysis)

    @property
    def odds_alerts_enabled(self):
        return self.store.get("odds_alerts_enabled", self.settings.odds_alerts_enabled)

    @property
    def odds_alert_relative_increase(self):
        return self.store.get(
            "odds_alert_relative_increase", self.settings.odds_alert_relative_increase
        )

    def publication_issue(self, draft, late=False):
        if late and self.allow_delayed_analysis:
            return eligible_analysis(draft, self.settings.max_draft_age_seconds)
        return eligible_draft(
            draft, self.settings.max_draft_age_seconds, self.settings.max_game_time_seconds
        )

    async def live_drafts(self, force=False):
        async with self.live_lock:
            if (
                not force
                and self.live_cache
                and (utcnow() - self.live_cache[0]).total_seconds() < 15
            ):
                return self.live_cache[1]
            calls = [self.odota.live()]
            if self.steam:
                calls.append(self.steam.live())
            results = await asyncio.gather(*calls, return_exceptions=True)
            available = []
            for name, result in zip(("OpenDota live", "Steam live"), results, strict=False):
                if isinstance(result, Exception):
                    self.store.health(name, False, type(result).__name__)
                    available.append([])
                else:
                    self.store.health(name, True, f"В источнике {len(result)} турнирных карт")
                    available.append(result)
            if all(isinstance(result, Exception) for result in results):
                raise RuntimeError("Все источники текущих драфтов недоступны")
            drafts = merge_live_drafts(
                available[0],
                available[1] if len(available) > 1 else [],
                self.settings.max_draft_age_seconds,
            )
            self.live_cache = (utcnow(), drafts)
            return drafts

    async def close(self):
        self.stop.set()
        if self.tournament_task and not self.tournament_task.done():
            self.tournament_task.cancel()
            await asyncio.gather(self.tournament_task, return_exceptions=True)
        await self.http.close()

    def update_tournament_calendar(self):
        events = {int(e["league_id"]): e for e in self.calendar}
        for event in self.store.tournaments():
            if event["league_id"]:
                # A Dota ticket can be shared by several stages; keep the widest calendar.
                previous = events.get(event["league_id"])
                if (
                    previous
                    and previous["end_date"] >= event["start_date"]
                    and event["end_date"] >= previous["start_date"]
                ):
                    event = {
                        **event,
                        "market_keywords": event.get("market_keywords")
                        or previous.get("market_keywords", []),
                        "start_date": min(previous["start_date"], event["start_date"]),
                        "end_date": max(previous["end_date"], event["end_date"]),
                    }
                events[event["league_id"]] = event
        self.calendar = list(events.values())

    async def refresh_tournaments(self):
        now = utcnow()
        self.store.set("tournament_catalog_checked_at", now.isoformat())
        try:
            events = await self.liquipedia.tournaments(now)
            try:
                markets = await self.poly.discover()
            except Exception as exc:
                markets = []
                self.store.health("Расписание Polymarket", False, type(exc).__name__)
            events = enrich_markets(events, markets, now)
            for event in events:
                self.store.remember_tournament(event)
            self.store.set("tournament_catalog_current", [event["source_key"] for event in events])
            self.update_tournament_calendar()
            self.store.health(
                "Новые турниры · Liquipedia",
                True,
                f"Tier 1/2: {len(events)}; проверены даты и уровень каждого события",
            )
            self.store.set("tournament_catalog_updated_at", utcnow().isoformat())
        except Exception as exc:
            self.store.health("Новые турниры · Liquipedia", False, type(exc).__name__)

    def start_tournament_refresh(self, force=False):
        if self.tournament_task and not self.tournament_task.done():
            return self.tournament_task
        checked = timestamp(self.store.get("tournament_catalog_checked_at"))
        if not force and checked and 0 <= (utcnow() - checked).total_seconds() < 1800:
            return None
        self.tournament_task = asyncio.create_task(self.refresh_tournaments())
        return self.tournament_task

    async def notify_tournaments(self, repeat=False):
        from .admin_ui import tournament_alert

        if not self.bot:
            return
        today, until = tournament_notice_window(utcnow())
        events = sorted(
            self.store.tournaments(), key=lambda event: (event["start_date"], event["id"])
        )
        verified = self.store.get("tournament_catalog_current")
        for admin in sorted(self.settings.admins):
            sent = 0
            for event in events:
                if (
                    event["tier"] not in (1, 2)
                    or event["end_date"] < today
                    or event["start_date"] > until
                    or event["enabled"]
                    and not repeat
                    or verified is not None
                    and event["source_key"] not in verified
                ):
                    continue
                notice = self.store.tournament_notice(event["id"], admin)
                if notice and notice["state"] == "failed" and not repeat:
                    updated = timestamp(notice["updated_at"])
                    if updated and (utcnow() - updated).total_seconds() < 1800:
                        continue
                if not self.store.begin_tournament_notice(event["id"], admin, repeat=repeat):
                    continue
                screen = tournament_alert(self, event["id"], admin)
                try:
                    message = await self.bot.send_message(
                        admin, screen.text, reply_markup=screen.keyboard
                    )
                    self.store.finish_tournament_notice(
                        event["id"], admin, "sent", message.message_id
                    )
                except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
                    self.store.finish_tournament_notice(event["id"], admin, "failed")
                    self.store.health("Уведомления о турнирах", False, type(exc).__name__)
                    break
                except Exception as exc:
                    self.store.finish_tournament_notice(event["id"], admin, "uncertain")
                    self.store.health("Уведомления о турнирах", False, type(exc).__name__)
                    break
                sent += 1
                if sent >= 3 and not repeat:
                    break
                if repeat:
                    await asyncio.sleep(1)

    async def refresh_catalog(self):
        try:
            leagues = await self.odota.leagues()
            for row in leagues:
                self.store.league(
                    int(row["leagueid"]), row.get("name") or f"Турнир {row['leagueid']}"
                )
            self.store.health("Каталог турниров", True, f"{len(leagues)} турниров")
        except Exception as exc:
            self.store.health("Каталог турниров", False, str(exc))
        try:
            self.heroes = {int(row["id"]): row for row in await self.odota.heroes()}
            (self.settings.data_dir / "heroes.json").write_text(
                json.dumps(self.heroes, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            path = self.settings.data_dir / "heroes.json"
            if path.exists():
                self.heroes = {
                    int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items()
                }
            self.store.health("Герои", False, str(exc))
        try:
            self.patches = await self.odota.patches()
            self.store.set("patches", self.patches)
            profiles = await self.odota.players()
            self.player_names = {
                int(p["account_id"]): p.get("name") or p.get("personaname") or "" for p in profiles
            }
        except Exception as exc:
            self.store.health("Профили и патчи", False, str(exc))
        await self.refresh_league_activity()

    async def refresh_league_activity(self, force=False):
        checked = timestamp(self.store.get("league_activity_checked_at"))
        if not force and checked and 0 <= (utcnow() - checked).total_seconds() < 300:
            return
        if self.activity_lock.locked():
            return
        async with self.activity_lock:
            results = await asyncio.gather(
                asyncio.wait_for(self.live_drafts(force=force), 8),
                asyncio.wait_for(self.odota.pro_matches(), 8),
                return_exceptions=True,
            )
            live, recent = results
            if not isinstance(live, Exception) or not isinstance(recent, Exception):
                snapshot = activity_snapshot(
                    [] if isinstance(live, Exception) else live,
                    [] if isinstance(recent, Exception) else recent,
                )
                for identity, cached in self.store.get("league_activity", {}).items():
                    played = timestamp(cached.get("last_match_at"))
                    if played and utcnow() - timedelta(days=7) <= played <= utcnow():
                        target = snapshot.setdefault(identity, {})
                        target["last_match_at"] = max(
                            target.get("last_match_at", ""), played.isoformat()
                        )
                    updated = timestamp(cached.get("live_updated_at"))
                    if (
                        isinstance(live, Exception)
                        and updated
                        and 0 <= (utcnow() - updated).total_seconds() <= 180
                    ):
                        snapshot.setdefault(identity, {})["live_updated_at"] = updated.isoformat()
                self.store.set("league_activity", snapshot)
                known = {r["id"] for r in self.store.leagues()}
                if {int(k) for k in snapshot} - known:
                    try:
                        for row in await asyncio.wait_for(self.odota.leagues(), 8):
                            self.store.league(
                                int(row["leagueid"]), row.get("name") or f"Турнир {row['leagueid']}"
                            )
                    except Exception:
                        pass
                complete = not any(isinstance(r, Exception) for r in results)
                self.store.health(
                    "Текущие турниры",
                    complete,
                    "Проверены live и последние про-матчи"
                    if complete
                    else "Один из источников недоступен; использованы доступные данные и календарь",
                )
            else:
                self.store.health(
                    "Текущие турниры",
                    False,
                    "Источник недоступен; показаны сохранённые данные и подтверждённые даты",
                )
            self.store.set("league_activity_checked_at", utcnow().isoformat())

    def online_history(self, observed_at, model=None):
        """Persist causal updates, so restart doesn't forget earlier completed games."""
        model = model or self.model
        active = model is self.model
        version = model.raw["version"]
        key = "online_history" if active else f"shadow_history:{version}"
        state = self.live_state if active else self.shadow_live_state
        if not state or state["version"] != version:
            saved = self.store.get(key)
            state = (
                saved
                if saved and saved.get("version") == version
                else {
                    "version": version,
                    "history": model.raw["history"],
                    "applied": [],
                    "as_of": model.raw["history_cutoff"],
                }
            )
        if active:
            self.live_state = state
        else:
            self.shadow_live_state = state
        history = History(state["history"])
        applied = set(state["applied"])
        cutoff = timestamp(model.raw["history_cutoff"])
        pending = []
        for row in self.store.finished():
            if row["id"] in applied:
                continue
            result = json.loads(row["result"])
            start = timestamp(result.get("start_time"))
            confirmed = timestamp(result.get("confirmed_at"))
            duration = result.get("duration")
            draft = Draft.from_dict(json.loads(row["draft"]))
            if not start or not confirmed or not duration or not draft.complete:
                continue
            finish = start + timedelta(seconds=int(duration) + 60)
            available = max(finish, confirmed)
            if finish > cutoff and available <= observed_at:
                pending.append((available, row["id"], draft, result))
        if pending:
            for available, identity, draft, result in sorted(
                pending, key=lambda item: (item[0], item[1])
            ):
                context = dict(result.get("context") or {})
                if history.glicko_teams is not None:
                    context["_result_available_at"] = available.timestamp()
                history.update(draft, result["radiant_win"], context)
                applied.add(identity)
                state["as_of"] = max(timestamp(state["as_of"]), available).isoformat()
            state["history"] = history.export()
            state["applied"] = sorted(applied)
            self.store.set(key, state)
        return history, state["as_of"]

    def record_shadow(self, prediction):
        """Record a candidate before the outcome, without changing published forecasts."""
        model = self.shadow_model
        if not model:
            return
        try:
            model.reload()
            if not model.ready or model.raw["version"] == prediction.model_version:
                return
            key = f"shadow_forecast:{prediction.draft.match_id}"
            if self.store.get(key):
                return
            history, as_of = self.online_history(prediction.draft.observed_at, model=model)
            shadow = model.predict(prediction.draft, history=history, history_as_of=as_of)
            self.store.set(
                key,
                {
                    "model_version": shadow.model_version,
                    "compared_to": prediction.model_version,
                    "recorded_at": utcnow().isoformat(),
                    "draft_observed_at": prediction.draft.observed_at.isoformat(),
                    "history_as_of": shadow.history_as_of,
                    "radiant_probability": shadow.radiant_probability,
                    "draft_probability": shadow.draft_probability,
                    "threshold": self.min_probability,
                },
            )
            self.store.health("Сравнение моделей", True, "Параллельный прогноз сохранён")
        except Exception as exc:
            self.store.health("Сравнение моделей", False, type(exc).__name__)

    def automatic_market_event(self, draft, events, aliases):
        # Manual restrictions take priority, including an explicitly empty filter.
        override = self.store.get(f"poly_league:{draft.league_id}")
        keywords = self.market_keywords(draft.league_id, draft.observed_at)
        league_name = next(
            (r["name"] for r in self.store.leagues() if r["id"] == draft.league_id), ""
        )
        candidates = []
        for event in events:
            if override is not None or keywords:
                verified = any(
                    normalized(keyword) in normalized(event.get("title", ""))
                    for keyword in keywords
                )
            else:
                verified = self.poly.tournament_matches(event, league_name)
            if verified:
                candidates.append(event)
        return self.poly.find_event(draft, candidates, aliases)

    async def hero_assets(self, draft: Draft):
        for player in draft.radiant + draft.dire:
            path = self.assets / f"{player.hero_id}.png"
            hero = self.heroes.get(player.hero_id, {})
            image_path = str(hero.get("img") or "").split("?")[0]
            if path.exists() or not image_path.startswith("/apps/dota2/"):
                continue
            try:
                response = await self.http.client.get(
                    "https://cdn.cloudflare.steamstatic.com" + image_path, timeout=3
                )
                response.raise_for_status()
                if len(response.content) <= 2_000_000:
                    path.write_bytes(response.content)
            except Exception:
                # A missing portrait is displayed as a hero ID; it doesn't change predictions.
                pass

    async def collect_quotes(self, draft: Draft, include_fonbet=True) -> list:
        quotes = []
        status = {"code": "market_disabled", "map_number": draft.map_number, "event_id": None}
        if self.store.get("source:Polymarket", True):
            try:
                binding = self.store.get(f"poly:{draft.match_id}")
                if not binding and draft.series_id and draft.series_id != "0":
                    binding = self.store.get(f"poly_series:{draft.series_id}")
                aliases = self.store.get("team_aliases", {})
                if binding:
                    event = await self.poly.event(str(binding))
                else:
                    events = await self.poly.discover()
                    event = self.automatic_market_event(draft, events, aliases)
                    if event:
                        # Discovery is cached for two minutes; refresh the matched event.
                        current = await self.poly.event(str(event["id"]))
                        event = self.automatic_market_event(draft, [current], aliases)
                if event:
                    status["event_id"] = str(event["id"])
                    quotes.extend(await self.poly.quotes(draft, event, aliases))
                    _, issue = self.poly.select_market(draft, event, aliases)
                    status["code"] = "available" if quotes else issue or "prices_unavailable"
                    if quotes and not binding:
                        self.store.set(f"poly:{draft.match_id}", str(event["id"]))
                else:
                    status["code"] = "market_unmatched"
                self.store.health(
                    "Polymarket",
                    bool(quotes),
                    f"Карта {draft.map_number}: {len(quotes)} котировок"
                    if quotes
                    else MARKET_ISSUES[status["code"]],
                )
            except Exception as exc:
                status["code"] = "market_source_error"
                self.store.health("Polymarket", False, str(exc))
        self.store.set(
            f"market_status:{draft.match_id}", {**status, "checked_at": utcnow().isoformat()}
        )
        if self.feed and not self.fonbet:
            try:
                quotes.extend(await self.feed.quotes(draft))
                self.store.health("Odds feed", True, "Источник ответил")
            except Exception as exc:
                self.store.health("Odds feed", False, str(exc))
        if include_fonbet and self.fonbet:
            if self.store.get("source:Fonbet", True):
                names = self.fonbet_league_names(draft.league_id)
                saved, issue = self.fonbet.quotes(draft, names)
                quotes.extend(saved)
            else:
                issue = "fonbet_disabled"
            self.store.set(
                f"fonbet_status:{draft.match_id}",
                {"code": issue, "checked_at": utcnow().isoformat()},
            )
        return [
            q
            for q in quotes
            if self.store.get(f"source:{q.source}", True)
            and (
                q.fresh(utcnow(), self.settings.quote_max_age_seconds) or valid_reference(q, draft)
            )
        ]

    def fonbet_league_names(self, league_id=None):
        selected = [
            r["name"]
            for r in self.store.leagues(league_id is None)
            if league_id is None or r["id"] == league_id
        ]
        selected += [
            keyword
            for event in self.calendar
            if (league_id is None and self.store.enabled(event["league_id"]))
            or event["league_id"] == league_id
            for keyword in event.get("market_keywords", [])
        ]
        return sorted(set(selected))

    def market_keywords(self, league_id, moment=None):
        override = self.store.get(f"poly_league:{league_id}")
        if override is not None:
            return [override] if override else []
        day = (moment or utcnow()).astimezone(MOSCOW).date().isoformat()
        return [
            keyword
            for event in self.calendar
            if int(event["league_id"]) == int(league_id)
            and event["start_date"] <= day <= event["end_date"]
            for keyword in event.get("market_keywords", [])
        ]

    async def complete_price_history(self, p, result):
        key = f"price_history:{p.draft.match_id}"
        if self.store.get(key):
            return
        start = timestamp(result.get("start_time"))
        duration = result.get("duration")
        if (
            not start
            or not duration
            or not any(
                q.source == "Polymarket" and q.price_kind == "market" and q.token_id
                for q in p.quotes
            )
        ):
            return
        end = start + timedelta(seconds=int(duration))
        try:
            since = max(start, p.draft.observed_at, min(q.observed_at for q in p.quotes))
            snapshots = await self.poly.history(p.quotes, since, end)
            groups = {}
            for q in snapshots:
                groups.setdefault(q.observed_at, []).append(q)
            for when, quotes in sorted(groups.items()):
                self.store.save_quotes(p.draft.match_id, quotes, observed_at=when)
            self.store.set(key, True)
        except Exception as exc:
            self.store.health("История Polymarket", False, type(exc).__name__)

    async def process_draft(self, draft: Draft) -> int | None:
        if not self.store.enabled(draft.league_id):
            return None
        draft = draft.with_team_aliases(self.store.get("team_aliases", {}))
        if draft.map_number is None:
            draft = draft.with_confirmed_map(self.store.draft(draft.match_id))
        map_number = self.store.get(f"map:{draft.match_id}", draft.map_number)

        def profiles(players):
            positions = self.store.get(f"positions:{draft.match_id}", {})
            return tuple(
                replace(
                    p,
                    name=p.name or self.player_names.get(p.account_id, ""),
                    position=positions.get(str(p.account_id), p.position),
                )
                for p in players
            )

        draft = replace(
            draft,
            map_number=map_number,
            patch=draft.patch
            if draft.patch is not None
            else patch_for_time(self.patches, draft.observed_at),
            radiant=profiles(draft.radiant),
            dire=profiles(draft.dire),
        )
        timing_status = draft_timing(draft, self.settings.max_game_time_seconds)
        late = timing_status != "early"
        reason = self.publication_issue(draft, late)
        self.store.observe(draft, "skipped" if reason else "observed", reason or "")
        if (
            reason
            or self.store.has_forecast(draft.match_id)
            or self.store.match(draft.match_id)["result"]
        ):
            return None
        if not draft.map_number:
            self.store.mark(draft.match_id, "waiting", "Ожидаем подтверждения номера карты")
            return None
        if late and self.steam:
            try:
                result = await self.steam.match(draft.match_id, draft.league_id)
            except Exception:
                result = {}
            if str(result.get("match_id")) == draft.match_id and isinstance(
                result.get("radiant_win"), bool
            ):
                self.store.result(
                    draft.match_id,
                    {
                        "radiant_win": result["radiant_win"],
                        "duration": result.get("duration"),
                        "start_time": result.get("start_time"),
                        "confirmed_at": utcnow().isoformat(),
                        "source": "Steam",
                    },
                )
                self.store.mark(draft.match_id, "finished", "карта уже завершена")
                return None
        self.model.reload()
        if not self.model.ready:
            self.store.mark(draft.match_id, "waiting_model", "модель ещё не обучена")
            return None
        try:
            history, as_of = self.online_history(draft.observed_at)
            p = self.model.predict(draft, history=history, history_as_of=as_of)
        except ValueError as exc:
            self.store.mark(draft.match_id, "skipped", str(exc))
            return None
        p.late_analysis = late
        p.timing_status = timing_status
        p.signal = p.favorite_probability >= self.min_probability
        if not p.signal and not self.publish_all_maps:
            self.store.mark(draft.match_id, "skipped", "нет достаточно выраженного фаворита")
            return None
        p.quotes = await self.collect_quotes(draft)
        from .paper_bank import initial_source

        selected_source = initial_source(p, utcnow(), self.settings.quote_max_age_seconds)
        p.analysis = {
            **p.analysis,
            "market_status": self.store.get(f"market_status:{draft.match_id}", {}),
            "initial_odds_source": selected_source,
            "fonbet_status": self.store.get(f"fonbet_status:{draft.match_id}", {}),
        }
        p.league_name = next(
            (r["name"] for r in self.store.leagues() if r["id"] == draft.league_id), ""
        )
        self.store.save_quotes(draft.match_id, p.quotes)
        if self.store.get("duration_enabled", False):
            for q in p.quotes:
                if q.market == "duration_total" and q.line is not None:
                    p.totals.append(
                        {
                            "line": q.line,
                            "outcome": q.outcome,
                            "probability": self.model.duration_probability(p, q.line, q.outcome),
                            "source": q.source,
                            "odds": q.odds,
                        }
                    )
        else:
            p.duration_seconds = None
            p.duration_interval = None
        forecast_id = self.store.add_forecast(p)
        if forecast_id:
            self.record_shadow(p)
            self.store.mark(draft.match_id, "preview", "")
            if self.automatic and not self.paused and self.model.publication_ready and self.chat_id:
                await self.publish(forecast_id)
            else:
                await self.preview(forecast_id)
        return forecast_id

    def saved_prediction(self, payload: str) -> Prediction:
        p = Prediction.from_dict(json.loads(payload))
        # An old forecast may predate map-number confirmation. Only fill identity
        # metadata from the exact same match and roster; never recompute its score.
        if not p.draft.map_number:
            p.draft = p.draft.with_confirmed_map(self.store.draft(p.draft.match_id))
        if not p.league_name:
            p.league_name = next(
                (r["name"] for r in self.store.leagues() if r["id"] == p.draft.league_id), ""
            )
        p.draft = p.draft.with_team_aliases(self.store.get("team_aliases", {}))
        p.late_analysis = separate_timing(p)
        return p

    async def preview(self, forecast_id: int):
        if not self.bot:
            return
        row = self.store.forecast(forecast_id)
        p = self.saved_prediction(row["payload"])
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Опубликовать", callback_data=f"publish:{forecast_id}"
                    ),
                    InlineKeyboardButton(text="Разбор", callback_data=f"detail:{forecast_id}"),
                ]
            ]
        )
        await self.hero_assets(p.draft)
        body = render_card(p, self.heroes, self.assets)
        for admin in sorted(self.settings.admins):
            try:
                await self.bot.send_photo(
                    admin,
                    BufferedInputFile(body, filename="draft.png"),
                    caption="<b>Предварительный просмотр</b>\n"
                    + caption(p, self.settings.quote_max_age_seconds, self.heroes),
                    reply_markup=keyboard,
                )
            except TelegramAPIError as exc:
                self.store.health("Telegram admin", False, type(exc).__name__)

    async def publish(self, forecast_id: int) -> str:
        row = self.store.forecast(forecast_id)
        if not row:
            return "Прогноз не найден."
        if row["state"] == "superseded":
            return f"Прогноз уже обработан: {row['state']}."
        if not self.bot or not self.destinations:
            return "Подключите группу или канал в разделе «Куда публиковать»."
        if self.paused:
            return "Публикации на паузе."
        if row["result"]:
            return "Карта уже завершена. Запоздалый прогноз не публикуется."
        p = self.saved_prediction(row["payload"])
        if not p.draft.map_number:
            return "Ожидаем подтверждения номера карты. Прогноз пока не публикуется."
        if not self.store.enabled(p.draft.league_id):
            return "Турнир выключен."
        if not self.model.publication_ready or not p.publication_ready:
            return "Модель пока не прошла минимальный порог проверки. Доступен предварительный просмотр; отчёт — /model."
        reason = self.publication_issue(p.draft, p.late_analysis)
        if (
            reason
            or (utcnow() - p.draft.observed_at).total_seconds()
            > self.settings.max_draft_age_seconds
        ):
            return "Данные устарели: прогноз не будет опубликован."
        # Freeze both image and text before starting the remote send.
        await self.hero_assets(p.draft)
        image = render_card(p, self.heroes, self.assets)
        if (
            self.publication_issue(p.draft, p.late_analysis)
            or (utcnow() - p.draft.observed_at).total_seconds()
            > self.settings.max_draft_age_seconds
        ):
            return "Во время подготовки карточки данные устарели. Прогноз не опубликован."
        plans = self.store.plan_publication(forecast_id, self.destinations)
        body = caption(p, self.settings.quote_max_age_seconds, self.heroes)
        sent, failed, uncertain = 0, 0, 0
        for target in plans:
            chat_id, topic_id = target["destination"], target["topic_id"]
            if self.paused or chat_id not in {t["chat_id"] for t in self.destinations}:
                continue
            if (
                self.publication_issue(p.draft, p.late_analysis)
                or (utcnow() - p.draft.observed_at).total_seconds()
                > self.settings.max_draft_age_seconds
            ):
                break
            delivery = self.store.begin_delivery(forecast_id, "forecast", chat_id, topic_id)
            if not delivery:
                continue
            try:
                msg = await self.bot.send_photo(
                    chat_id,
                    BufferedInputFile(image, filename="draft.png"),
                    caption=body,
                    message_thread_id=topic_id,
                )
            except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
                self.store.finish_delivery(delivery, "failed", error=type(exc).__name__)
                self.store.health(f"Telegram {chat_id}", False, type(exc).__name__)
                failed += 1
                continue
            except Exception as exc:
                self.store.finish_delivery(delivery, "uncertain", error=type(exc).__name__)
                self.store.health(
                    f"Telegram {chat_id}",
                    False,
                    f"Неопределённая отправка прогноза #{forecast_id}; нужна сверка",
                )
                uncertain += 1
                continue
            self.store.finish_delivery(delivery, "sent", msg.message_id)
            self.capture_paper_bets()
            self.store.health(f"Telegram {chat_id}", True, "Карточка доставлена")
            sent += 1
        messages = [
            f"Прогноз опубликован: {sent} мест."
            if sent
            else "Новых публикаций нет; отправка уже обработана или требует сверки."
        ]
        if failed:
            messages.append(
                f"Telegram отклонил {failed} отправок. Проверьте права бота в разделе «Куда публиковать»."
            )
        if uncertain:
            messages.append(
                f"Исход отправки неизвестен для {uncertain} мест. Проверьте посты и используйте /confirm {forecast_id} forecast MESSAGE_ID CHAT_ID. Повторной отправки не будет."
            )
        return "\n".join(messages)

    async def publish_results(self):
        if not self.bot or self.paused:
            return
        for row in self.store.publications():
            if (
                self.paused
                or not self.destination_enabled(row["destination"])
                or not row["result"]
                or self.store.delivery_sent(row["id"], "result", row["destination"])
            ):
                continue
            result = json.loads(row["result"])
            if not isinstance(result.get("radiant_win"), bool):
                continue
            p = self.saved_prediction(row["payload"])
            if not p.quotes:
                from .domain import Quote

                attached = self.store.get(f"quote_attached:{row['id']}:{row['destination']}", [])
                start = timestamp(result.get("start_time"))
                finish = (
                    start + timedelta(seconds=int(result["duration"]))
                    if start and result.get("duration")
                    else None
                )
                p.quotes = [
                    Quote.from_dict(q)
                    for q in attached
                    if finish and timestamp(q["observed_at"]) <= finish
                ]
            await self.complete_price_history(p, result)
            delivery = self.store.begin_delivery(
                row["id"], "result", row["destination"], row["destination_topic_id"]
            )
            if not delivery:
                continue
            try:
                msg = await self.bot.send_message(
                    row["destination"],
                    result_text(p, result, self.store.quote_snapshots(p.draft.match_id)),
                    reply_parameters=ReplyParameters(message_id=row["post_id"]),
                    message_thread_id=row["destination_topic_id"],
                )
                self.store.finish_delivery(delivery, "sent", msg.message_id)
            except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
                self.store.finish_delivery(delivery, "failed", error=type(exc).__name__)
            except Exception as exc:
                self.store.finish_delivery(delivery, "uncertain", error=type(exc).__name__)

    async def collect_results(self):
        unresolved = set(self.store.unresolved())
        if not unresolved:
            # Delivery can have failed after the last result was already saved.
            await self.publish_results()
            return
        checked = confirmed = 0
        sources = {"OpenDota": [0, 0], "Steam": [0, 0]}

        def save(match_id, row, source):
            if (
                not isinstance(row, dict)
                or str(row.get("match_id")) != match_id
                or not isinstance(row.get("radiant_win"), bool)
            ):
                return False
            self.store.result(
                match_id,
                {
                    "radiant_win": row["radiant_win"],
                    "duration": row.get("duration"),
                    "start_time": row.get("start_time"),
                    "context": match_context(row),
                    "confirmed_at": utcnow().isoformat(),
                    "source": source,
                    "method": row.get("result_method"),
                    "match_seq_num": row.get("match_seq_num"),
                },
            )
            unresolved.discard(match_id)
            return True

        try:
            now = utcnow()
            if (
                not self.results_recent_checked_at
                or (now - self.results_recent_checked_at).total_seconds() >= 300
            ):
                self.results_recent_checked_at = now
                try:
                    recent = await self.odota.pro_matches()
                    sources["OpenDota"][0] += 1
                except Exception:
                    sources["OpenDota"][1] += 1
                    recent = []
                for row in recent:
                    match_id = str(row.get("match_id"))
                    if match_id in unresolved and isinstance(row.get("radiant_win"), bool):
                        try:
                            details = await self.odota.match(match_id)
                            sources["OpenDota"][0] += 1
                            if (
                                str(details.get("match_id")) == match_id
                                and details.get("radiant_win") is row["radiant_win"]
                            ):
                                row = {**row, **details}
                        except Exception:
                            sources["OpenDota"][1] += 1
                    if match_id in unresolved and save(match_id, row, "OpenDota"):
                        confirmed += 1
            for match_id in self.store.result_candidates():
                self.store.result_checked(match_id)
                checked += 1
                draft = self.store.draft(match_id)
                providers = ([("Steam", self.steam)] if self.steam else []) + [
                    ("OpenDota", self.odota)
                ]
                for source, provider in providers:
                    try:
                        row = (
                            await provider.match(match_id, draft.league_id)
                            if source == "Steam"
                            else await provider.match(match_id)
                        )
                        sources[source][0] += 1
                    except Exception:
                        sources[source][1] += 1
                        continue
                    if save(match_id, row, source):
                        confirmed += 1
                        break
            for source, (responses, errors) in sources.items():
                if responses or errors:
                    self.store.health(
                        f"Результаты · {source}",
                        bool(responses),
                        f"Ответов: {responses}; ошибок: {errors}",
                    )
            available = sum(counts[0] for counts in sources.values())
            if checked or confirmed or any(counts[1] for counts in sources.values()):
                self.store.health(
                    "Результаты",
                    bool(available),
                    f"Подтверждено: {confirmed}; проверено: {checked}; ожидают исхода: {len(unresolved)}"
                    if available
                    else "Источники результатов недоступны; очередь сохранена, повторим проверку",
                )
        except Exception as exc:
            self.store.health("Результаты", False, type(exc).__name__)
        await self.publish_results()

    async def track_market_prices(self):
        tracked = 0
        for row in self.store.forecasts("published", limit=100):
            if row["result"]:
                continue
            p = self.saved_prediction(row["payload"])
            if (
                p.draft.map_number
                and 0 <= (utcnow() - p.draft.observed_at).total_seconds() <= 6 * 3600
            ):
                quotes = await self.collect_quotes(p.draft, include_fonbet=False)
                self.store.save_quotes(p.draft.match_id, quotes)
                if not p.quotes and quotes:
                    await self.attach_missing_quotes(row["id"], p, quotes)
                await self.notify_price_increase(row["id"], p, quotes)
                tracked += 1
                if tracked >= 8:
                    break

    async def notify_price_increase(self, forecast_id, p, quotes):
        from .domain import Quote
        from .price_alerts import alert_text, comparable_quote, favorite_quote, identity, increased

        now = utcnow()
        if not self.bot or self.paused or not self.automatic or not self.odds_alerts_enabled:
            return
        if (
            not self.store.enabled(p.draft.league_id)
            or not self.store.get("source:Polymarket", True)
            or (now - p.draft.observed_at).total_seconds() < 180
        ):
            return
        if self.store.match(p.draft.match_id)["result"]:
            return
        current = favorite_quote(p, quotes, self.settings.quote_max_age_seconds, now)
        if not current:
            return
        for target in self.store.publications(forecast_id):
            if (
                self.paused
                or not self.automatic
                or not self.odds_alerts_enabled
                or not self.store.get("source:Polymarket", True)
                or not self.store.enabled(p.draft.league_id)
                or target["result"]
            ):
                return
            if not self.destination_enabled(target["destination"]):
                continue
            key = f"odds_alert:{forecast_id}:{target['destination']}"
            state = self.store.get(key)
            if not state or state["market_key"] != identity(current):
                attached = [
                    Quote.from_dict(q)
                    for q in self.store.get(
                        f"quote_attached:{forecast_id}:{target['destination']}", []
                    )
                ]
                baseline = (
                    current
                    if state
                    else comparable_quote(p, p.quotes or attached, current) or current
                )
                state = {
                    "market_key": identity(current),
                    "reference": baseline.odds,
                    "count": state["count"] if state else 0,
                    "last_at": state["last_at"] if state else None,
                }
                self.store.set(key, state)
            previous_at = timestamp(state["last_at"])
            if state["count"] >= self.settings.odds_alert_max_per_map or (
                previous_at
                and (now - previous_at).total_seconds() < self.settings.odds_alert_cooldown_seconds
            ):
                continue
            if not increased(
                state["reference"],
                current.odds,
                self.odds_alert_relative_increase,
                self.settings.odds_alert_min_increase,
            ):
                continue
            kind = f"odds_alert:{state['count'] + 1}"
            delivery = self.store.begin_delivery(
                forecast_id, kind, target["destination"], target["destination_topic_id"]
            )
            if not delivery:
                continue
            # Save before sending: a lost reply or crash must not cause a duplicate.
            next_state = {
                **state,
                "reference": current.odds,
                "count": state["count"] + 1,
                "last_at": now.isoformat(),
            }
            self.store.set(key, next_state)
            try:
                msg = await self.bot.send_message(
                    target["destination"],
                    alert_text(p, state["reference"], current),
                    reply_parameters=ReplyParameters(message_id=target["post_id"]),
                    message_thread_id=target["destination_topic_id"],
                )
                self.store.finish_delivery(delivery, "sent", msg.message_id)
                self.store.health("Движение кэфов", True, "Уведомление доставлено")
            except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
                self.store.finish_delivery(delivery, "failed", error=type(exc).__name__)
                self.store.set(key, {**state, "last_at": now.isoformat()})
                self.store.health("Движение кэфов", False, type(exc).__name__)
            except Exception as exc:
                self.store.finish_delivery(delivery, "uncertain", error=type(exc).__name__)
                self.store.health(
                    "Движение кэфов", False, "Исход отправки неизвестен; повтор не отправляется"
                )

    async def attach_missing_quotes(self, forecast_id, p, quotes):
        if not self.bot or self.paused:
            return
        displayed = replace(p, quotes=quotes)
        if not market_snapshot(displayed, self.settings.quote_max_age_seconds)[0]:
            return
        image = render_card(displayed, self.heroes, self.assets)
        for target in self.store.publications(forecast_id):
            key = f"quote_attached:{forecast_id}:{target['destination']}"
            if not self.destination_enabled(target["destination"]) or self.store.get(key):
                continue
            try:
                from aiogram.types import InputMediaPhoto

                await self.bot.edit_message_media(
                    chat_id=target["destination"],
                    message_id=target["post_id"],
                    media=InputMediaPhoto(
                        media=BufferedInputFile(image, filename="draft.png"),
                        caption=caption(
                            displayed, self.settings.quote_max_age_seconds, self.heroes
                        ),
                    ),
                )
                self.store.set(key, [q.to_dict() for q in quotes])
            except TelegramAPIError as exc:
                self.store.health("Обновление котировок", False, type(exc).__name__)

    async def reports(self):
        if not self.bot or not self.destinations or not self.automatic or self.paused:
            return
        for target in self.destinations:
            if not self.paused and target["chat_id"] in {t["chat_id"] for t in self.destinations}:
                await self.reports_for(target)

    async def morning_brief(self):
        from .brief import brief_caption, scheduled_series
        from .reports import report_payload

        now = utcnow().astimezone(MOSCOW)
        hour = self.store.get("morning_brief_hour", self.settings.morning_brief_hour)
        if (
            now.hour != hour
            or not self.bot
            or self.paused
            or not self.automatic
            or not self.store.get("morning_brief_enabled", self.settings.morning_brief_enabled)
        ):
            return
        period = f"brief:{now.date()}"
        targets = [
            t
            for t in self.destinations
            if self.store.report_state(period, t["chat_id"]) in (None, "failed")
        ]
        if not targets:
            return
        rules = [
            {
                "league_id": row["id"],
                "name": row["name"],
                "keywords": self.market_keywords(row["id"], now),
            }
            for row in self.store.leagues(enabled_only=True)
        ]
        rules = [rule for rule in rules if rule["keywords"]]
        if not rules:
            return
        try:
            series = scheduled_series(await self.poly.discover(), rules, now)
            if not series:
                return
            midnight = datetime.combine(now.date(), time.min, MOSCOW)
            for target in targets:
                since = (midnight - timedelta(days=1)).astimezone(utcnow().tzinfo).isoformat()
                until = midnight.astimezone(utcnow().tzinfo).isoformat()
                previous = report_payload(self.store, since, until, destination=target["chat_id"])
                previous["late"] = report_payload(
                    self.store, since, until, destination=target["chat_id"], late_analysis=True
                )
                await self.send_report(period, brief_caption(series, previous), target=target)
        except Exception as exc:
            self.store.health("Утренний анонс", False, type(exc).__name__)

    async def reports_for(self, target):
        now = utcnow().astimezone(MOSCOW)
        from .reports import (
            daily_report_hour,
            public_report_payload,
            render_report,
            report_caption,
            weekly_caption,
        )

        hour = daily_report_hour(self.store.get("daily_report_hour", 0))
        days = list(range(1, 8)) if now.hour >= hour else list(range(2, 8))
        for days_ago in days:
            day = now.date() - timedelta(days=days_ago)
            start = datetime.combine(day, time.min, MOSCOW).astimezone(utcnow().tzinfo)
            end = start + timedelta(days=1)
            summary = public_report_payload(
                self.store,
                start.isoformat(),
                end.isoformat(),
                destination=target["chat_id"],
            )
            if (
                not summary["published"]
                or summary["pending"]
                or summary.get("additional", {}).get("pending")
            ):
                continue
            period = f"day:{day}:final"
            if summary["late_analysis"]:
                period += ":delayed"
            previous = (f"day:{day}:final", f"day:{day}:final:delayed")
            if any(
                key != period
                and self.store.report_state(key, target["chat_id"])
                in ("sent", "sending", "uncertain", "archived")
                for key in previous
            ):
                continue
            await self.send_report(period, report_caption(summary), render_report(summary), target)
        monday = now.date() - timedelta(days=now.weekday())
        due = datetime.combine(monday, time(self.store.get("weekly_report_hour", 12)), MOSCOW)
        if now >= due:
            end = datetime.combine(monday, time.min, MOSCOW).astimezone(utcnow().tzinfo)
            start = end - timedelta(days=7)
            week_key = f"week:{start.date()}:final"
            if any(
                self.store.report_state(key, target["chat_id"])
                in ("sent", "sending", "uncertain", "archived")
                for key in (week_key, week_key + ":delayed")
            ):
                return
            summary = public_report_payload(
                self.store,
                start.isoformat(),
                end.isoformat(),
                "Итоги недели",
                target["chat_id"],
            )
            if (
                summary["published"]
                and not summary["pending"]
                and not summary.get("additional", {}).get("pending")
            ):
                period = f"week:{start.date()}:final" + (
                    ":delayed" if summary["late_analysis"] else ""
                )
                other = f"week:{start.date()}:final" + (
                    "" if summary["late_analysis"] else ":delayed"
                )
                if self.store.report_state(other, target["chat_id"]) in (
                    "sent",
                    "sending",
                    "uncertain",
                    "archived",
                ):
                    return
                await self.send_report(
                    period,
                    weekly_caption(summary),
                    render_report(summary),
                    target,
                )

    async def send_report(self, period: str, body: str, image: bytes | None = None, target=None):
        target = target or (self.destinations[0] if self.destinations else None)
        if not target or not self.bot:
            return
        chat_id, topic_id = target["chat_id"], target["topic_id"]
        if self.paused or not self.destination_enabled(chat_id):
            return
        if not self.store.begin_report(period, chat_id):
            return
        try:
            if image:
                msg = await self.bot.send_photo(
                    chat_id,
                    BufferedInputFile(image, filename="report.png"),
                    caption=body,
                    message_thread_id=topic_id,
                )
            else:
                msg = await self.bot.send_message(chat_id, body, message_thread_id=topic_id)
            self.store.finish_report(period, chat_id, "sent", msg.message_id)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter):
            self.store.finish_report(period, chat_id, "failed")
        except Exception:
            self.store.finish_report(period, chat_id, "uncertain")

    def capture_paper_bets(self):
        from .paper_bank import PaperBank

        try:
            PaperBank(self.store).capture(
                self.settings.admins, max_quote_age=self.settings.quote_max_age_seconds
            )
        except Exception as exc:
            self.store.health("Виртуальный банк", False, type(exc).__name__)

    async def paper_bets(self):
        from .paper_bank import PaperBank, notification_text

        bank = PaperBank(self.store)
        self.capture_paper_bets()
        try:
            bank.settle(self.settings.admins)
        except Exception as exc:
            self.store.health("Виртуальный банк", False, type(exc).__name__)
            return
        if not self.bot:
            return
        for notification in bank.notifications(self.settings.admins):
            if not bank.begin_notification(notification["id"]):
                continue
            try:
                message = await self.bot.send_message(
                    notification["owner_id"],
                    notification_text(notification, self.store.get("team_aliases", {})),
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="💰 Мой виртуальный банк", callback_data="ui:paper"
                                )
                            ]
                        ]
                    ),
                )
            except TelegramRetryAfter as exc:
                bank.finish_notification(
                    notification["id"],
                    "planned",
                    error=type(exc).__name__,
                    retry_after=exc.retry_after + 1,
                )
                break
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                bank.finish_notification(notification["id"], "failed", error=type(exc).__name__)
            except Exception as exc:
                bank.finish_notification(notification["id"], "uncertain", error=type(exc).__name__)
            else:
                bank.finish_notification(notification["id"], "sent", message.message_id)

    async def tick(self):
        if self.tick_lock.locked():
            return
        async with self.tick_lock:
            self.tick_count += 1
            self.start_tournament_refresh()
            self.model.reload()
            await self.refresh_league_activity()
            if self.fonbet and self.store.get("source:Fonbet", True):
                await self.fonbet.refresh(self.fonbet_league_names())
            if self.store.leagues(enabled_only=True):
                try:
                    drafts = await self.live_drafts()
                    coverage = []
                    for draft in drafts:
                        if not self.store.enabled(draft.league_id):
                            continue
                        draft = draft.with_team_aliases(self.store.get("team_aliases", {}))
                        try:
                            await self.process_draft(draft)
                        except Exception as exc:
                            self.store.observe(draft, "error", type(exc).__name__)
                            log.warning("Draft %s failed: %s", draft.match_id, type(exc).__name__)
                        row = self.store.match(draft.match_id)
                        coverage.append(
                            {
                                "match_id": draft.match_id,
                                "league_id": draft.league_id,
                                "teams": f"{draft.radiant_name} — {draft.dire_name}",
                                "source": draft.source_name,
                                "status": row["status"],
                                "reason": row["reason"],
                                "forecast": self.store.has_forecast(draft.match_id),
                            }
                        )
                    self.store.set(
                        "live_coverage", {"checked_at": utcnow().isoformat(), "matches": coverage}
                    )
                    self.store.health(
                        "Разборы",
                        True,
                        f"Выбранные карты: {len(coverage)}; с анализом: {sum(r['forecast'] for r in coverage)}",
                    )
                except Exception as exc:
                    self.store.health("Разборы", False, type(exc).__name__)
            await self.collect_results()
            await self.paper_bets()
            if self.automatic and not self.paused:
                for identity in self.store.pending_publications():
                    row = self.store.forecast(identity)
                    p = Prediction.from_dict(json.loads(row["payload"]))
                    if (
                        not row["result"]
                        and row["state"] != "superseded"
                        and 0
                        <= (utcnow() - p.draft.observed_at).total_seconds()
                        < self.settings.max_draft_age_seconds
                    ):
                        await self.publish(identity)
            await self.track_market_prices()
            await self.morning_brief()
            await self.reports()
            repeat = self.store.get("tournament_alert_repeat_requested", False)
            if repeat:
                self.store.set("tournament_alert_repeat_requested", False)
            await self.notify_tournaments(repeat=repeat)

    async def run(self):
        from .runtime import heartbeat

        await self.refresh_catalog()
        while not self.stop.is_set():
            try:
                await self.tick()
                heartbeat(self.settings)
            except Exception as exc:
                # Store a concise type, avoiding HTTP URLs that may contain credentials.
                self.store.health("Worker", False, type(exc).__name__)
                log.warning("Worker cycle failed: %s", type(exc).__name__)
                heartbeat(self.settings, False)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.settings.poll_seconds)
            except TimeoutError:
                pass
