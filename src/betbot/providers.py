import asyncio
import json
import math
import re
import time
import unicodedata
from dataclasses import replace
from typing import Any

import httpx

from .domain import Draft, Player, Quote, timestamp, utcnow
from .timing import draft_timing

BOOKMAKERS = ("1win", "BetBoom", "Winline", "PARI", "Fonbet")


class ProviderError(Exception):
    pass


class HTTP:
    def __init__(self, proxy: str | None = None):
        self.client = httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            proxy=proxy,
            headers={"User-Agent": "Betbot/0.1 (Dota analysis)"},
        )
        self._odota_lock = asyncio.Lock()
        self._last_odota = 0.0

    async def close(self):
        await self.client.aclose()

    async def get(
        self, url: str, params: dict | None = None, headers: dict | None = None, attempts: int = 3
    ):
        for attempt in range(attempts):
            if "api.opendota.com" in url:
                async with self._odota_lock:
                    await asyncio.sleep(max(0, 1.1 - (time.monotonic() - self._last_odota)))
                    self._last_odota = time.monotonic()
            try:
                response = await self.client.get(url, params=params, headers=headers)
                if response.status_code in (429, 502, 503, 504) and attempt < attempts - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                raise ProviderError(
                    f"HTTP {exc.response.status_code}: {exc.request.url.host}"
                ) from None
            except (httpx.RequestError, ValueError):
                if attempt == attempts - 1:
                    raise ProviderError(f"Недоступен источник {httpx.URL(url).host}") from None
                await asyncio.sleep(2 ** (attempt + 1))
        raise ProviderError("Источник не ответил")


def normalized(value: str) -> str:
    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", value).casefold())


def json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except ValueError:
            pass
    return []


def patch_for_time(patches: list[dict], moment) -> int | None:
    moment = timestamp(moment)
    available = [(timestamp(p.get("date")), p.get("id")) for p in patches]
    eligible = [
        (date, identity)
        for date, identity in available
        if date and moment and date <= moment and isinstance(identity, int)
    ]
    return max(eligible, default=(None, None))[1]


def match_context(raw: dict) -> dict:
    """Post-game measurements; never construct a forecast Draft from these fields."""
    players = sorted(raw.get("players", []), key=lambda p: p.get("player_slot", 0))
    result = {
        "accounts": [p.get("account_id") or 0 for p in players],
        "slots": [p.get("player_slot") for p in players],
        "lane_roles": [p.get("lane_role") for p in players],
        "gpms": [p.get("gold_per_min") for p in players],
    }
    for target, source in (("gold10", "gold_t"), ("xp10", "xp_t"), ("lh10", "lh_t")):
        values = []
        for p in players:
            times, samples = p.get("times") or [], p.get(source) or []
            index = times.index(600) if 600 in times else -1
            values.append(samples[index] if 0 <= index < len(samples) else None)
        result[target] = values
    return result


def parse_live(raw: dict, observed_at=None) -> Draft:
    def players(team: int) -> tuple[Player, ...]:
        members = [p for p in raw.get("players", []) if p.get("team") == team]
        return tuple(
            Player(
                int(p.get("account_id") or 0), int(p.get("hero_id") or 0), str(p.get("name") or "")
            )
            for p in sorted(members, key=lambda p: p.get("team_slot", 0))
        )

    map_number = None
    if raw.get("radiant_series_wins") is not None and raw.get("dire_series_wins") is not None:
        map_number = int(raw["radiant_series_wins"]) + int(raw["dire_series_wins"]) + 1
    return Draft(
        match_id=str(raw.get("match_id") or ""),
        league_id=int(raw.get("league_id") or 0),
        radiant_id=int(raw.get("team_id_radiant") or 0),
        dire_id=int(raw.get("team_id_dire") or 0),
        radiant_name=str(raw.get("team_name_radiant") or ""),
        dire_name=str(raw.get("team_name_dire") or ""),
        radiant=players(0),
        dire=players(1),
        observed_at=observed_at or utcnow(),
        source_updated_at=timestamp(raw.get("last_update_time")),
        game_time=int(raw.get("game_time") or 0),
        series_id=str(raw.get("series_id") or ""),
        map_number=map_number,
        patch=raw.get("patch"),
        delay_seconds=int(raw.get("delay") or 0),
        clock_is_delayed=True,
        clock_known=isinstance(raw.get("game_time"), (int, float))
        and isinstance(raw.get("delay"), (int, float)),
        active=not bool(raw.get("deactivate_time")),
    )


def parse_steam_live(raw: dict, observed_at=None) -> Draft:
    """Only identities and heroes enter the draft; scoreboard performance is excluded."""
    now = observed_at or utcnow()
    scoreboard = raw.get("scoreboard") or {}
    names = {int(p.get("account_id") or 0): p.get("name") or "" for p in raw.get("players", [])}

    def players(side, team):
        members = [p for p in raw.get("players", []) if p.get("team") == team]
        # Slot is an identifier, never a lane inferred from current-game performance.
        slots = {
            p.get("account_id"): p.get("player_slot", 999)
            for p in (scoreboard.get(side) or {}).get("players", [])
        }
        return tuple(
            Player(
                int(p.get("account_id") or 0),
                int(p.get("hero_id") or 0),
                str(names.get(int(p.get("account_id") or 0), "")),
            )
            for p in sorted(
                members,
                key=lambda p: (slots.get(p.get("account_id"), 999), int(p.get("account_id") or 0)),
            )
        )

    rteam, dteam = raw.get("radiant_team") or {}, raw.get("dire_team") or {}
    wins = [raw.get("radiant_series_wins"), raw.get("dire_series_wins")]
    return Draft(
        match_id=str(raw.get("match_id") or ""),
        league_id=int(raw.get("league_id") or 0),
        radiant_id=int(rteam.get("team_id") or 0),
        dire_id=int(dteam.get("team_id") or 0),
        radiant_name=str(rteam.get("team_name") or ""),
        dire_name=str(dteam.get("team_name") or ""),
        radiant=players("radiant", 0),
        dire=players("dire", 1),
        observed_at=now,
        # This endpoint exposes no snapshot timestamp. This records the direct read,
        # not a claim that delayed scoreboard telemetry is real-time.
        source_updated_at=now,
        game_time=int(scoreboard.get("duration") or 0),
        map_number=sum(wins) + 1 if all(isinstance(w, int) and w >= 0 for w in wins) else None,
        delay_seconds=int(raw.get("stream_delay_s") or 0),
        source_name="Steam",
        clock_is_delayed=True,
        clock_known=isinstance(scoreboard.get("duration"), (int, float))
        and isinstance(raw.get("stream_delay_s"), (int, float)),
    )


class Steam:
    base = "https://api.steampowered.com/IDOTA2Match_570"

    def __init__(self, http: HTTP, key: str):
        self.http, self.key = http, key
        self._history = {}
        self._history_targets = {}

    async def live(self) -> list[Draft]:
        league, top = await asyncio.gather(
            self.http.get(self.base + "/GetLiveLeagueGames/v1/", {"key": self.key}),
            self.http.get(self.base + "/GetTopLiveGame/v1/", {"key": self.key, "partner": 1}),
            return_exceptions=True,
        )
        games = (league.get("result") or {}).get("games") if isinstance(league, dict) else None
        top_games = top.get("game_list") if isinstance(top, dict) else None
        if not isinstance(games, list) and not isinstance(top_games, list):
            raise ProviderError("Steam не вернул список турнирных карт")
        now = utcnow()
        drafts = []
        for game in games or []:
            try:
                draft = parse_steam_live(game, now)
                if draft.match_id.isdigit() and draft.league_id > 0:
                    drafts.append(draft)
            except (TypeError, ValueError, KeyError):
                continue
        top_drafts = []
        for game in top_games or []:
            try:
                if int(game.get("league_id") or 0) > 0 and game.get("match_id"):
                    top_drafts.append(replace(parse_live(game, now), source_name="Steam Top"))
            except (TypeError, ValueError, KeyError):
                continue
        return merge_live_drafts(top_drafts, drafts)

    async def match(self, match_id: str, league_id: int | None = None) -> dict:
        if not match_id.isdigit():
            raise ProviderError("Некорректный match ID")
        if league_id:
            return await self._history_result(match_id, league_id)
        raw = await self.http.get(
            self.base + "/GetMatchDetails/v1/", {"key": self.key, "match_id": match_id}
        )
        return raw.get("result", {})

    async def _history_result(self, match_id: str, league_id: int) -> dict:
        # GetMatchDetails can return HTTP 500 even for completed tournament games.
        # Resolve a sequence number from this league, then demand the exact match ID.
        now = time.monotonic()
        cached_at, matches = self._history.get(league_id, (0, {}))
        if not cached_at or now - cached_at >= 60:
            matches = await self._league_history(league_id)
            self._history[league_id] = (now, matches)
        match = matches.get(match_id)
        # Older cards may have fallen out of the latest page. A newer missing ID
        # is usually still playing; it must not inherit the previous map's result.
        if not match and matches and int(match_id) < max(map(int, matches)):
            target = (league_id, match_id)
            cached_at, older = self._history_targets.get(target, (0, {}))
            if not cached_at or now - cached_at >= 300:
                page = await self._league_history(league_id, match_id)
                older = {match_id: page[match_id]} if match_id in page else {}
                if len(self._history_targets) >= 1000:
                    self._history_targets.pop(next(iter(self._history_targets)))
                self._history_targets[target] = (now, older)
            match = older.get(match_id)
        if not match or not isinstance(match.get("match_seq_num"), int):
            return {}
        raw = await self.http.get(
            self.base + "/GetMatchHistoryBySequenceNum/v1/",
            {
                "key": self.key,
                "start_at_match_seq_num": match["match_seq_num"],
                "matches_requested": 1,
            },
        )
        result = raw.get("result", {})
        for row in result.get("matches", []):
            if (
                str(row.get("match_id")) == match_id
                and row.get("leagueid") == league_id
                and row.get("match_seq_num") == match["match_seq_num"]
                and isinstance(row.get("radiant_win"), bool)
            ):
                return {**row, "result_method": "GetMatchHistoryBySequenceNum"}
        return {}

    async def _league_history(self, league_id, before=None):
        params = {"key": self.key, "league_id": league_id, "matches_requested": 100}
        if before:
            params["start_at_match_id"] = before
        raw = await self.http.get(self.base + "/GetMatchHistory/v1/", params)
        result = raw.get("result", {})
        if result.get("status") != 1 or not isinstance(result.get("matches"), list):
            raise ProviderError("Steam не вернул историю турнира")
        return {
            str(row["match_id"]): row
            for row in result["matches"]
            if str(row.get("match_id", "")).isdigit()
        }


def merge_live_drafts(opendota: list[Draft], steam: list[Draft], max_age=180) -> list[Draft]:
    """Steam adds coverage; matching fresh TopLiveGames supplies a delayed game clock."""
    combined = {d.match_id: d for d in opendota}
    for draft in steam:
        other = combined.get(draft.match_id)
        if other and other.source_updated_at:
            age = (draft.observed_at - other.source_updated_at).total_seconds()
            same_teams = (draft.league_id, draft.radiant_id, draft.dire_id) == (
                other.league_id,
                other.radiant_id,
                other.dire_id,
            )
            same_players = all(
                {(p.account_id, p.hero_id) for p in getattr(draft, side)}
                == {(p.account_id, p.hero_id) for p in getattr(other, side)}
                for side in ("radiant", "dire")
            )
            if same_teams and same_players and 0 <= age <= max_age and other.active:
                draft = replace(
                    draft,
                    map_number=draft.map_number
                    if draft.map_number is not None
                    else other.map_number,
                    series_id=other.series_id,
                    patch=other.patch,
                    game_time=other.game_time,
                    source_updated_at=other.source_updated_at,
                    clock_is_delayed=other.clock_is_delayed,
                    clock_known=other.clock_known,
                    delay_seconds=other.delay_seconds,
                    source_name="Steam + " + other.source_name,
                )
            elif 0 <= age <= max_age and other.complete and draft.complete:
                # Conflicting identities must not silently attach a market or a forecast.
                combined[draft.match_id] = replace(draft, active=False)
                continue
        if draft.complete or not other or not other.complete:
            combined[draft.match_id] = draft
    return list(combined.values())


class OpenDota:
    base = "https://api.opendota.com/api"

    def __init__(self, http: HTTP, api_key: str = ""):
        self.http = http
        self.key = {"api_key": api_key} if api_key else {}
        self._rate_limited_until = 0.0

    async def get(self, path: str, **params):
        if time.monotonic() < self._rate_limited_until:
            raise ProviderError("OpenDota: пауза после HTTP 429")
        try:
            return await self.http.get(self.base + path, {**self.key, **params})
        except ProviderError as exc:
            if str(exc).startswith("HTTP 429:"):
                self._rate_limited_until = time.monotonic() + 300
            raise

    async def live(self) -> list[Draft]:
        rows = await self.get("/live")
        now = utcnow()
        drafts = []
        for row in rows:
            try:
                if int(row.get("league_id") or 0) > 0 and row.get("match_id"):
                    drafts.append(parse_live(row, now))
            except (TypeError, ValueError):
                continue
        return drafts

    async def leagues(self) -> list[dict]:
        return await self.get("/leagues")

    async def heroes(self) -> list[dict]:
        return await self.get("/heroStats")

    async def patches(self) -> list[dict]:
        return await self.get("/constants/patch")

    async def players(self) -> list[dict]:
        return await self.get("/proPlayers")

    async def match(self, match_id: str) -> dict:
        if not match_id.isdigit():
            raise ProviderError("Некорректный match ID")
        return await self.get(f"/matches/{match_id}")

    async def pro_matches(self) -> list[dict]:
        return await self.get("/proMatches")

    async def training_data(self, limit: int = 5000) -> list[dict]:
        limit = max(300, min(int(limit), 20000))
        rows, cursor = [], None
        while len(rows) < limit:
            page_size = min(1000, limit - len(rows))
            page = await self._training_page(page_size, cursor)
            if not page:
                break
            rows.extend(page)
            next_cursor = min(int(r["match_id"]) for r in page)
            if cursor is not None and next_cursor >= cursor:
                raise ProviderError("OpenDota вернул повторную страницу истории")
            cursor = next_cursor
        patches = await self.patches()
        for row in rows:
            row["patch"] = patch_for_time(patches, row["start_time"])
        return sorted(rows, key=lambda row: (int(row["start_time"]), int(row["match_id"])))

    async def _training_page(self, limit: int, cursor: int | None) -> list[dict]:
        columns = "m.match_id,m.start_time,m.radiant_team_id,m.dire_team_id,m.radiant_win,m.duration,m.leagueid,m.series_id,m.radiant_team_name,m.dire_team_name"
        before = f"AND match_id < {int(cursor)} " if cursor is not None else ""
        sql = (
            f"SELECT {columns},array_agg(pm.hero_id ORDER BY pm.player_slot) AS heroes,"
            "array_agg(pm.account_id ORDER BY pm.player_slot) AS accounts,"
            "array_agg(pm.player_slot ORDER BY pm.player_slot) AS slots,"
            "array_agg(pm.lane_role ORDER BY pm.player_slot) AS lane_roles,"
            "array_agg(pm.gold_per_min ORDER BY pm.player_slot) AS gpms,"
            "array_agg(pm.is_roaming ORDER BY pm.player_slot) AS roaming,"
            "array_agg(pm.gold_t[array_position(pm.times,600)] ORDER BY pm.player_slot) AS gold10,"
            "array_agg(pm.xp_t[array_position(pm.times,600)] ORDER BY pm.player_slot) AS xp10,"
            "array_agg(pm.lh_t[array_position(pm.times,600)] ORDER BY pm.player_slot) AS lh10 "
            "FROM (SELECT * FROM matches WHERE game_mode=2 AND lobby_type=1 "
            "AND radiant_team_id>0 AND dire_team_id>0 "
            f"{before}"
            f"ORDER BY match_id DESC LIMIT {limit}) m JOIN player_matches pm USING(match_id) "
            f"GROUP BY {columns} ORDER BY m.start_time ASC"
        )
        response = await self.get("/explorer", sql=sql)
        if response.get("err"):
            raise ProviderError("OpenDota Explorer отклонил запрос истории")
        return response.get("rows", [])


class Polymarket:
    name = "Polymarket"

    def __init__(self, http: HTTP):
        self.http = http
        self.events: list[dict] = []
        self.loaded_at = None

    async def discover(self, force: bool = False) -> list[dict]:
        if not force and self.loaded_at and (utcnow() - self.loaded_at).total_seconds() < 120:
            return self.events
        sports = await self.http.get("https://gamma-api.polymarket.com/sports")
        sport = next((s for s in sports if s.get("sport") == "dota2"), None)
        if not sport:
            raise ProviderError("В каталоге Polymarket не найден Dota 2")
        tag = sport.get("primaryTagId")
        if not tag:
            raise ProviderError("В каталоге Dota 2 отсутствует tag ID")
        events = []
        # Bounded pagination; exact event ID can also be attached manually.
        for offset in range(0, 1000, 100):
            page = await self.http.get(
                "https://gamma-api.polymarket.com/events",
                {
                    "tag_id": tag,
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                    "_t": int(utcnow().timestamp()),
                },
            )
            if not isinstance(page, list):
                raise ProviderError("Изменилась схема каталога Polymarket")
            events.extend(page)
            if len(page) < 100:
                break
        self.events, self.loaded_at = events, utcnow()
        return events

    async def event(self, event_id: str) -> dict:
        if not event_id.isdigit():
            raise ProviderError("Ожидается числовой event ID")
        # Gamma's CDN otherwise caches this response for five minutes, including
        # the list of map markets and their acceptingOrders state.
        return await self.http.get(
            f"https://gamma-api.polymarket.com/events/{event_id}",
            {"_t": int(utcnow().timestamp())},
        )

    @staticmethod
    def displayed_price(book, max_spread=0.1):
        def levels(side):
            result = []
            for level in book.get(side, []):
                try:
                    price, size = float(level["price"]), float(level["size"])
                    if (
                        math.isfinite(price)
                        and math.isfinite(size)
                        and 0 <= price <= 1
                        and size > 0
                    ):
                        result.append((price, size))
                except (KeyError, ValueError, TypeError):
                    continue
            return result

        asks, bids = levels("asks"), levels("bids")
        ask = min((p for p, _ in asks), default=None)
        bid = max((p for p, _ in bids), default=None)
        price = None
        if ask is not None and bid is not None and 0 <= ask - bid <= max_spread + 1e-9:
            price = (ask + bid) / 2
        elif ask is None or bid is None or ask >= bid:
            try:
                price = float(book.get("last_trade_price"))
            except (ValueError, TypeError):
                pass
        if price is None or not math.isfinite(price) or not 0 < price < 1:
            return None
        liquidity = sum(size for p, size in asks if p == ask) * ask if ask else None
        return price, (1 / ask if ask and ask < 1 else None), liquidity

    @staticmethod
    def find_event(draft: Draft, events: list[dict], aliases: dict | None = None) -> dict | None:
        aliases = aliases or {}
        rname = normalized(aliases.get(str(draft.radiant_id), draft.radiant_name))
        dname = normalized(aliases.get(str(draft.dire_id), draft.dire_name))
        candidates = []
        for event in events:
            names = {normalized(t.get("name", "")) for t in event.get("teams", [])}
            start = timestamp(event.get("startTime"))
            if names != {rname, dname} or not start:
                continue
            if abs((draft.observed_at - start).total_seconds()) <= 12 * 3600:
                candidates.append(event)
        # The caller also verifies tournament identity before automatic binding.
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def tournament_matches(event: dict, league_name: str) -> bool:
        """Match a known Dota ticket to tournament metadata, ignoring edition numbers."""
        words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", league_name).casefold())
        ignored = {"season", "сезон", "the", "by"}
        words = [w for w in words if not w.isdigit() and w not in ignored]
        if not words or not any(len(w) >= 4 for w in words):
            return False
        metadata = event.get("eventMetadata") or {}
        label = " ".join(
            [str(event.get("title", ""))]
            + [str(metadata.get(k, "")) for k in ("league", "serie", "tournament")]
        )
        actual = set(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", label).casefold()))
        return set(words) <= actual

    @staticmethod
    def map_market(raw: dict, map_number: int, require_open=True) -> bool:
        if require_open and (raw.get("closed") or not raw.get("acceptingOrders", False)):
            return False
        label = f"{raw.get('groupItemTitle', '')} {raw.get('question', '')}"
        numbers = re.findall(r"(?:game|map|карта)\s*(\d+)\s*(?:winner|победитель)", label, re.I)
        return (
            bool(numbers)
            and {int(n) for n in numbers} == {map_number}
            and raw.get("sportsMarketType") in ("child_moneyline", "moneyline")
        )

    @classmethod
    def deciding_map_context(cls, draft, event, outcome_aliases=None):
        """A live BO3 1-1 / BO5 2-2 decider shares the played winner with the series."""
        if (
            not draft.active
            or not draft.complete
            or event.get("live") is not True
            or event.get("ended")
            or event.get("closed")
            or not event.get("id")
            or event.get("seriesSlug") != "dota-2"
            or cls.find_event(draft, [event], outcome_aliases) is None
        ):
            return None
        updated = timestamp(event.get("updatedAt"))
        if not updated or not 0 <= (utcnow() - updated).total_seconds() <= 120:
            return None
        score = re.fullmatch(
            r"[^|]*\|\s*(\d+)\s*-\s*(\d+)\s*\|\s*Bo\s*(\d+)\s*", str(event.get("score", "")), re.I
        )
        period = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(event.get("period", "")))
        if not score or not period:
            return None
        a, b, best_of = map(int, score.groups())
        current, total = map(int, period.groups())
        formats = {int(n) for n in re.findall(r"\bBO\s*(\d+)\b", str(event.get("title", "")), re.I)}
        if (
            best_of not in (1, 3, 5)
            or (formats and formats != {best_of})
            or current != total
            or total != best_of
            or draft.map_number != best_of
            or a != b
            or a != best_of // 2
        ):
            return None
        return {
            "event_id": str(event["id"]),
            "best_of": best_of,
            "series_score": [a, b],
            "map_number": current,
            "event_updated_at": updated.isoformat(),
            "underlying_market": "series_winner",
        }

    @classmethod
    def select_market(cls, draft, event, outcome_aliases=None):
        if not draft.map_number:
            return None, "map_unknown"
        aliases = outcome_aliases or {}
        rname = normalized(aliases.get(str(draft.radiant_id), draft.radiant_name))
        dname = normalized(aliases.get(str(draft.dire_id), draft.dire_name))
        candidates = [
            m for m in event.get("markets", []) if cls.map_market(m, draft.map_number, False)
        ]
        context = None
        if not candidates:
            context = cls.deciding_map_context(draft, event, outcome_aliases)
            if not context:
                return None, "map_market_missing"
            candidates = [
                m
                for m in event.get("markets", [])
                if m.get("sportsMarketType") == "moneyline"
                and normalized(m.get("groupItemTitle", "")) in ("matchwinner", "serieswinner")
            ]
            if not candidates:
                return None, "map_market_missing"
        matching = [
            m
            for m in candidates
            if not m.get("closed") and m.get("acceptingOrders") and m.get("active") is not False
        ]
        if not matching:
            return None, "map_market_closed"
        if len(matching) != 1:
            return None, "market_ambiguous"
        market = matching[0]
        outcomes = json_list(market.get("outcomes"))
        ids = (
            json_list(market.get("positionIds"))
            if market.get("version") == "v2"
            else json_list(market.get("clobTokenIds"))
        )
        if (
            len(outcomes) != 2
            or len(ids) != 2
            or {normalized(x) for x in outcomes} != {rname, dname}
        ):
            return None, "market_identity"
        return ({**market, "_deciding_map": context} if context else market), ""

    async def quotes(
        self, draft: Draft, event: dict, outcome_aliases: dict | None = None
    ) -> list[Quote]:
        market, _ = self.select_market(draft, event, outcome_aliases)
        if not market:
            return []
        aliases = outcome_aliases or {}
        rname = normalized(aliases.get(str(draft.radiant_id), draft.radiant_name))
        outcomes = json_list(market.get("outcomes"))
        ids = json_list(
            market.get("positionIds")
            if market.get("version") == "v2"
            else market.get("clobTokenIds")
        )
        result, midpoints = [], []
        traded = False
        context = market.get("_deciding_map") or {}
        rules = "polymarket:" + str(market.get("conditionId") or market["id"])
        if context:
            rules += f":decider:BO{context['best_of']}:map{draft.map_number}"
        for name, asset_id in zip(outcomes, ids, strict=True):
            book = await self.http.get(
                "https://clob.polymarket.com/book", {"token_id": str(asset_id)}
            )
            # /book can repeat the market's latest trade for BOTH outcome tokens.
            # Use its levels for a tight midpoint, but fetch the last trade by
            # token when the spread is wide. Never use the sibling's trade price.
            selected = self.displayed_price({**book, "last_trade_price": None})
            midpoint = self.displayed_price({**book, "last_trade_price": None}, max_spread=1)
            if not selected:
                trade = await self.http.get(
                    "https://clob.polymarket.com/last-trade-price", {"token_id": str(asset_id)}
                )
                selected = self.displayed_price(
                    {
                        **book,
                        "last_trade_price": trade.get("price")
                        if trade.get("side") in ("BUY", "SELL")
                        else None,
                    }
                )
                traded = traded or bool(selected)
            # Book timestamp often means last mutation, not this read. Observed_at is the
            # current direct read; changed timestamp is retained in market metadata separately.
            for selection, target, basis in (
                (selected, result, ""),
                (midpoint, midpoints, "midpoint_fallback"),
            ):
                if not selection:
                    continue
                price, ask_odds, liquidity = selection
                target.append(
                    Quote(
                        source=self.name,
                        match_id=draft.match_id,
                        market="map_winner",
                        outcome="radiant" if normalized(name) == rname else "dire",
                        odds=1 / price,
                        observed_at=utcnow(),
                        source_updated_at=None,
                        map_number=draft.map_number,
                        rules=rules,
                        url="https://polymarket.com/event/" + str(event.get("slug", "")),
                        gross=True,
                        liquidity=liquidity,
                        market_id=str(market["id"]),
                        price_kind="market",
                        token_id=str(asset_id),
                        ask_odds=ask_odds,
                        price_basis=basis,
                        market_basis="series_decider" if context else "",
                        market_context=context,
                    )
                )
        # Last trades can be stale or inconsistent after settlement even while Gamma
        # still marks the market open. Never display two mutually exclusive 99% prices.
        if len(result) == 2 and abs(sum(1 / q.odds for q in result) - 1) <= 0.10 + 1e-9:
            return result
        # Last trades of the two tokens can be from different moments. The current
        # two-sided book supplies a coherent market reference even in that case.
        # Mark it explicitly; do not turn untraded 50/50 placeholders into prices.
        if (
            traded
            and len(midpoints) == 2
            and abs(sum(1 / q.odds for q in midpoints) - 1) <= 0.02 + 1e-9
        ):
            return midpoints
        return result if len(result) < 2 else []

    async def history(self, quotes, start, end):
        """One-minute chart samples; never used as a forecast-time quote."""
        result = []
        if start > end:
            return result
        for q in quotes:
            if (
                q.source != self.name
                or q.market != "map_winner"
                or q.price_kind != "market"
                or q.price_basis == "midpoint_fallback"
                or not q.token_id
            ):
                continue
            data = await self.http.get(
                "https://clob.polymarket.com/prices-history",
                {
                    "market": q.token_id,
                    "startTs": int(start.timestamp()),
                    "endTs": int(end.timestamp()),
                    "fidelity": 1,
                },
            )
            for tick in data.get("history", []):
                try:
                    when, price = timestamp(tick["t"]), float(tick["p"])
                except (KeyError, TypeError, ValueError):
                    continue
                if when and start <= when <= end and math.isfinite(price) and 0 < price < 1:
                    result.append(
                        replace(
                            q,
                            odds=1 / price,
                            observed_at=when,
                            source_updated_at=when,
                            ask_odds=None,
                            liquidity=None,
                            price_origin="history",
                        )
                    )
        return result


async def steam_map_numbers(http: HTTP, key: str) -> dict[str, int]:
    result = await http.get(
        "https://api.steampowered.com/IDOTA2Match_570/GetLiveLeagueGames/v1/", {"key": key}
    )
    numbers = {}
    for game in result.get("result", {}).get("games", []):
        if (
            game.get("match_id")
            and isinstance(game.get("radiant_series_wins"), int)
            and isinstance(game.get("dire_series_wins"), int)
        ):
            numbers[str(game["match_id"])] = (
                game["radiant_series_wins"] + game["dire_series_wins"] + 1
            )
    return numbers


class OddsFeed:
    """Explicit match IDs and market semantics supplied by a connected JSON feed."""

    name = "Odds feed"

    def __init__(self, http: HTTP, url: str, api_key: str = ""):
        self.http, self.url, self.api_key = http, url, api_key

    async def quotes(self, draft: Draft) -> list[Quote]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        data = await self.http.get(self.url, {"match_id": draft.match_id}, headers)
        result = []
        for row in data.get("quotes", []):
            try:
                q = Quote.from_dict(row)
                if q.source not in BOOKMAKERS or q.match_id != draft.match_id:
                    continue
                if (
                    q.market not in ("map_winner", "duration_total")
                    or q.map_number != draft.map_number
                ):
                    continue
                # A feed must prove when its source was observed, not merely when we fetched it.
                if not q.source_updated_at or not q.rules:
                    continue
                if q.market == "map_winner" and (
                    q.outcome not in ("radiant", "dire") or q.line is not None
                ):
                    continue
                if q.market == "duration_total" and (
                    q.outcome not in ("over", "under")
                    or q.line is None
                    or not math.isfinite(q.line)
                    or q.line <= 0
                ):
                    continue
                result.append(replace(q, observed_at=utcnow()))
            except (TypeError, ValueError, KeyError):
                continue
        return result


class TheOddsAPI:
    """Coverage diagnostics only: a sports catalogue is not a Dota map odds feed."""

    def __init__(self, http: HTTP, key: str):
        self.http, self.key = http, key

    async def coverage(self) -> dict:
        sports = await self.http.get(
            "https://api.the-odds-api.com/v4/sports", {"apiKey": self.key, "all": "true"}
        )
        if not isinstance(sports, list):
            raise ProviderError("The Odds API: неверный ответ каталога видов спорта")
        dota = [
            {k: s.get(k) for k in ("key", "title", "active")}
            for s in sports
            if isinstance(s, dict)
            and "dota"
            in normalized(" ".join(str(s.get(k, "")) for k in ("key", "title", "description")))
        ]
        return {
            "ok": True,
            "connected": False,
            "sports_count": len(sports),
            "dota_sports": dota,
            "note": (
                "Dota найдена в каталоге; рынки отдельных карт ещё не проверены и не подключены"
                if dota
                else "Ключ принят, но Dota 2 отсутствует в полном каталоге; источник котировок Dota 2 не подключён"
            ),
        }


class OddsPapi:
    """Requires an explicit fixture/market mapping, never guesses vendor market IDs."""

    name = "OddsPapi"

    def __init__(self, http: HTTP, key: str, base: str):
        self.http, self.key, self.base = http, key, base.rstrip("/")
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def request(self, endpoint: str, params=None):
        # Errors are billable too. The quota-aware caller decides when to try again.
        async with self._request_lock:
            await asyncio.sleep(max(0, 2.1 - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            return await self.http.get(
                self.base + "/" + endpoint,
                {"apiKey": self.key, **(params or {})},
                attempts=1,
            )

    async def account(self) -> dict:
        raw = await self.request("account")
        active = [s for s in raw.get("subscriptions", []) if s.get("is_active")]
        current = next(
            (s for s in active if s.get("subscription_id") == raw.get("current_subscription_id")),
            active[0] if len(active) == 1 else {},
        )
        books = current.get("bookmakers") or {}
        limit, used = current.get("request_limit"), current.get("request_count")
        return {
            "authenticated": True,
            "dota_allowed": 16 in current.get("sport_ids", []),
            "fonbet_allowed": "fonbet" in books,
            "fonbet_live": bool(books.get("fonbet", {}).get("has_live_odds")),
            "request_limit": limit,
            "request_count": used,
            "remaining": max(0, limit - used)
            if isinstance(limit, int) and isinstance(used, int)
            else None,
        }

    async def bookmakers(self) -> list[dict]:
        result = await self.request("bookmakers")
        return result if isinstance(result, list) else result.get("bookmakers", [])

    async def quotes(self, draft: Draft, bindings: list[dict]) -> list[Quote]:
        quotes = []
        for binding in bindings:
            if (
                binding.get("source") not in BOOKMAKERS
                or binding.get("map_number") != draft.map_number
            ):
                continue
            response = await self.http.get(
                self.base + "/odds",
                {
                    "apiKey": self.key,
                    "fixtureId": binding["fixture_id"],
                    "bookmaker": binding["bookmaker_key"],
                    "oddsFormat": "decimal",
                },
            )
            book = response.get("bookmakerOdds", {}).get(binding["bookmaker_key"], {})
            market = book.get("markets", {}).get(str(binding["market_id"]), {})
            for outcome, outcome_id in binding["outcomes"].items():
                player = (
                    market.get("outcomes", {})
                    .get(str(outcome_id), {})
                    .get("players", {})
                    .get("0", {})
                )
                # changedAt is not a freshness timestamp. Refuse vendor records with
                # no explicit source observation, rather than freshening a stale cache.
                observed = timestamp(player.get("observedAt") or book.get("observedAt"))
                if not observed or not player.get("price"):
                    continue
                quotes.append(
                    Quote(
                        source=binding["source"],
                        match_id=draft.match_id,
                        market=binding.get("market", "map_winner"),
                        outcome=outcome,
                        odds=float(player["price"]),
                        observed_at=utcnow(),
                        source_updated_at=observed,
                        map_number=draft.map_number,
                        line=binding.get("line"),
                        rules=binding["rules"],
                        active=bool(book.get("bookmakerIsActive") and player.get("active")),
                        url=book.get("fixturePath", ""),
                        market_id=str(binding["market_id"]),
                    )
                )
        return quotes


def eligible_draft(draft: Draft, max_age: int, max_game_time: int) -> str | None:
    reason = eligible_analysis(draft, max_age)
    if reason:
        return reason
    stage = draft_timing(draft, max_game_time)
    if stage == "unverified":
        return "время карты неизвестно: нельзя подтвердить прогноз сразу после драфта"
    if stage == "late":
        return "карта уже идёт: снимок опоздал для прогноза после драфта"
    return None


def eligible_analysis(draft: Draft, max_age: int) -> str | None:
    if not draft.complete:
        return "неполный драфт или состав команд"
    if not draft.active:
        return "карта завершена или источники расходятся по составу"
    if not draft.source_updated_at:
        return "нет времени обновления live-источника"
    age = (utcnow() - draft.source_updated_at).total_seconds()
    if age < -5 or age > max_age:
        return "запоздалый снимок live-источника"
    if draft.delay_seconds < 0:
        return "некорректная задержка live-источника"
    return None
