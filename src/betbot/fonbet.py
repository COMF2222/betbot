"""Quota-limited collection of Fonbet prices BEFORE a series, never live offers."""

import asyncio
import hashlib
import math
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from .domain import Quote, timestamp, utcnow
from .providers import ProviderError, normalized

MOSCOW = ZoneInfo("Europe/Moscow")
ISSUES = {
    "fonbet_unmatched": "нет заранее сохранённого рынка этого матча у Fonbet",
    "fonbet_ambiguous": "несколько предматчевых рынков Fonbet подходят этому матчу",
    "fonbet_map_missing": "Fonbet не дал предматчевый рынок победителя этой карты",
    "fonbet_disabled": "сбор предматчевых коэффициентов Fonbet выключен",
    "fonbet_stale": "предматчевый снимок Fonbet не прошёл проверку времени",
}


def valid_reference(q, draft, at=None):
    """A past benchmark, explicitly distinct from an available post-draft offer."""
    at = at or draft.observed_at
    context = q.market_context
    start = timestamp(context.get("series_start_at"))
    return bool(
        q.source == "Fonbet"
        and q.price_origin == "prematch"
        and q.price_kind == "prematch_reference"
        and q.market == "map_winner"
        and q.match_id == draft.match_id
        and q.map_number == draft.map_number
        and q.outcome in ("radiant", "dire")
        and q.active
        and q.line is None
        and math.isfinite(q.odds)
        and 1 < q.odds <= 1_000_000
        and context.get("provider") == "OddsPapi"
        and context.get("stage") == "prematch"
        and context.get("fixture_id")
        and timestamp(context.get("captured_at")) == q.observed_at
        and q.source_updated_at == q.observed_at
        and start
        and q.observed_at < start
        and 0 <= (at - q.observed_at).total_seconds() <= 48 * 3600
        and abs((draft.observed_at - start).total_seconds()) <= 12 * 3600
    )


def league_matches(fixture, names):
    name = normalized(fixture.get("tournamentName", ""))
    return len(name) >= 4 and any(name in normalized(n) for n in names)


def map_catalogue(rows):
    result = {}
    for row in rows:
        if (
            row.get("sportId") != 16
            or row.get("marketType") != "moneyline"
            or row.get("playerProp")
            or row.get("handicap") != 0
            or row.get("marketLength") != 2
            or not re.fullmatch(r"p[1-5]", str(row.get("period", "")))
            or not re.fullmatch(
                r"(?:First|Second|Third|Fourth|Fifth) Map Winner(?: \(incl\. overtime\))?",
                row.get("marketName", ""),
            )
        ):
            continue
        outcomes = row.get("outcomes", [])
        if len(outcomes) != 2 or {o.get("outcomeName") for o in outcomes} != {"1", "2"}:
            continue
        number = row["period"][1:]
        ordinal = {"First": "1", "Second": "2", "Third": "3", "Fourth": "4", "Fifth": "5"}
        if ordinal.get(row["marketName"].split()[0]) != number or not row.get("marketId"):
            continue
        if len({str(o.get("outcomeId", "")) for o in outcomes}) != 2 or any(
            not o.get("outcomeId") for o in outcomes
        ):
            continue
        result.setdefault(number, []).append(
            {
                "market_id": str(row["marketId"]),
                "outcomes": {o["outcomeName"]: str(o["outcomeId"]) for o in outcomes},
                "name": row["marketName"],
            }
        )
    return {n: definitions[0] for n, definitions in result.items() if len(definitions) == 1}


class FonbetPrematch:
    def __init__(self, api, store, daily_limit=8, interval=86400):
        self.api, self.store = api, store
        self.daily_limit, self.interval = daily_limit, interval
        self.key_tag = hashlib.sha256(api.key.encode()).hexdigest()[:16]
        self.lock = asyncio.Lock()

    async def account(self, now=None):
        now = now or utcnow()
        saved = self.store.get("fonbet:account", {})
        checked = timestamp(saved.get("checked_at"))
        if (
            saved.get("key_tag") == self.key_tag
            and checked
            and 0 <= (now - checked).total_seconds() < 3600
        ):
            return saved
        account = {
            **await self.api.account(),
            "checked_at": now.isoformat(),
            "key_tag": self.key_tag,
        }
        self.store.set("fonbet:account", account)
        return account

    async def billed(self, endpoint, params, now):
        account = await self.account(now)
        if not account.get("dota_allowed") or not account.get("fonbet_allowed"):
            raise ProviderError("Тариф OddsPapi не разрешает Dota 2 / Fonbet")
        remaining = account.get("remaining")
        if not isinstance(remaining, int) or remaining <= 0:
            raise ProviderError("Лимит запросов OddsPapi исчерпан или не подтверждён")
        meter = self.store.get("fonbet:meter", {})
        day = now.astimezone(MOSCOW).date().isoformat()
        if meter.get("day") != day or meter.get("key_tag") != self.key_tag:
            meter = {"day": day, "used": 0, "key_tag": self.key_tag}
        if meter["used"] >= self.daily_limit:
            raise ProviderError("Достигнут дневной лимит запросов OddsPapi")
        # Reserve BEFORE the call, including failed responses and interrupted runs.
        self.store.set("fonbet:meter", {**meter, "used": meter["used"] + 1})
        self.store.set(
            "fonbet:account",
            {**account, "remaining": remaining - 1, "request_count": account["request_count"] + 1},
        )
        return await self.api.request(endpoint, params)

    async def refresh(self, league_names, now=None):
        now = now or utcnow()
        if not league_names or self.lock.locked():
            return
        async with self.lock:
            last = timestamp(self.store.get("fonbet:last_attempt"))
            success = timestamp(self.store.get("fonbet:last_success"))
            same_key = self.store.get("fonbet:prefetch_key") == self.key_tag
            same_leagues = self.store.get("fonbet:fixtures", {}).get("leagues") == sorted(
                league_names
            )
            slots = self.store.get("fonbet:slots", {})
            upcoming = self.store.get("fonbet:fixtures", {}).get("rows", [])
            due = [
                f
                for f in upcoming
                if f.get("sportId") == 16
                and league_matches(f, league_names)
                and (start := timestamp(f.get("startTime")))
                and 0 < (start - now).total_seconds() <= 300
                and slots.get(str(f.get("fixtureId"))) != f.get("startTime")
            ]
            if same_key and (
                (
                    not due
                    and same_leagues
                    and success
                    and 0 <= (now - success).total_seconds() < self.interval
                )
                or (
                    last
                    and (not success or last > success)
                    and 0 <= (now - last).total_seconds() < 900
                )
            ):
                return
            self.store.set("fonbet:last_attempt", now.isoformat())
            self.store.set("fonbet:prefetch_key", self.key_tag)
            try:
                await self.account(now)
                cached = self.store.get("fonbet:catalogue", {})
                catalogue_at = timestamp(cached.get("checked_at"))
                if not catalogue_at or not 0 <= (now - catalogue_at).total_seconds() < 7 * 86400:
                    rows = await self.billed("markets", {"language": "en"}, now)
                    definitions = map_catalogue(rows)
                    if not definitions:
                        raise ProviderError("OddsPapi не дал каталог рынков карт Dota 2")
                    cached = {"checked_at": now.isoformat(), "maps": definitions}
                    self.store.set("fonbet:catalogue", cached)
                fixtures = self.store.get("fonbet:fixtures", {})
                fetched = timestamp(fixtures.get("checked_at"))
                signature = sorted(league_names)
                if (
                    not fetched
                    or not 0 <= (now - fetched).total_seconds() < 86400
                    or fixtures.get("leagues") != signature
                ):
                    today = now.date()
                    rows = await self.billed(
                        "fixtures",
                        {
                            "sportId": 16,
                            "from": today.isoformat(),
                            "to": (today + timedelta(days=2)).isoformat(),
                            "hasOdds": "true",
                            "language": "en",
                        },
                        now,
                    )
                    if not isinstance(rows, list):
                        raise ProviderError("Неверный каталог матчей OddsPapi")
                    fixtures = {"checked_at": now.isoformat(), "leagues": signature, "rows": rows}
                    self.store.set("fonbet:fixtures", fixtures)
                due = [
                    f
                    for f in fixtures.get("rows", [])
                    if league_matches(f, league_names)
                    and (start := timestamp(f.get("startTime")))
                    and 0 < (start - now).total_seconds() <= 300
                    and slots.get(str(f.get("fixtureId"))) != f.get("startTime")
                ]
                ids = sorted(
                    {
                        str(f["tournamentId"])
                        for f in fixtures.get("rows", [])
                        if f.get("sportId") == 16
                        and f.get("tournamentId")
                        and league_matches(f, league_names)
                    }
                )
                if ids:
                    rows = await self.billed(
                        "odds-by-tournaments",
                        {
                            "tournamentIds": ",".join(ids),
                            "bookmakers": "fonbet",
                            "verbosity": 3,
                            "language": "en",
                        },
                        now,
                    )
                    if not isinstance(rows, list):
                        raise ProviderError("Неверный пакет котировок OddsPapi")
                    self.save_batch(rows, cached["maps"], league_names, utcnow())
                    # Batch replies can reveal later rounds added since daily discovery.
                    known = {
                        str(f["fixtureId"]): f
                        for f in fixtures.get("rows", [])
                        if f.get("fixtureId")
                    }
                    for row in rows:
                        if (
                            row.get("fixtureId")
                            and row.get("sportId") == 16
                            and league_matches(row, league_names)
                        ):
                            known[str(row["fixtureId"])] = {
                                k: v for k, v in row.items() if k != "bookmakerOdds"
                            }
                    fixtures = {**fixtures, "rows": list(known.values())}
                    self.store.set("fonbet:fixtures", fixtures)
                    # A successful batch without a map price is still one paid attempt.
                    slots = {
                        str(f["fixtureId"]): slots[str(f["fixtureId"])]
                        for f in fixtures.get("rows", [])
                        if str(f.get("fixtureId")) in slots
                    }
                    slots.update({str(f["fixtureId"]): f["startTime"] for f in due})
                    self.store.set("fonbet:slots", slots)
                self.store.set("fonbet:last_success", now.isoformat())
                saved = self.store.get("fonbet:snapshots", {})
                count = sum(len(s.get("maps", {})) for s in saved.values())
                self.store.health(
                    "Fonbet",
                    True,
                    f"До начала серии: сохранено {count} рынков карт; live по тарифу недоступен"
                    if not self.store.get("fonbet:account", {}).get("fonbet_live")
                    else f"Сохранено {count} предматчевых рынков карт",
                )
            except Exception as exc:
                self.store.health(
                    "Fonbet",
                    False,
                    str(exc) if isinstance(exc, ProviderError) else type(exc).__name__,
                )

    def save_batch(self, rows, definitions, league_names, captured):
        saved = self.store.get("fonbet:snapshots", {})
        saved = {
            k: v
            for k, v in saved.items()
            if (start := timestamp(v.get("startTime")))
            and abs((captured - start).total_seconds()) < 48 * 3600
        }
        for row in rows:
            start = timestamp(row.get("startTime"))
            book = (row.get("bookmakerOdds") or {}).get("fonbet", {})
            if (
                row.get("sportId") != 16
                or row.get("statusId") != 0
                or row.get("trueStartTime")
                or row.get("trueEndTime")
                or not row.get("fixtureId")
                or not row.get("participant1Name")
                or not row.get("participant2Name")
                or normalized(row["participant1Name"]) == normalized(row["participant2Name"])
                or not start
                or not 0 < (start - captured).total_seconds() <= 48 * 3600
                or not league_matches(row, league_names)
                or book.get("bookmakerIsActive") is not True
                or book.get("suspended") is not False
            ):
                continue
            identity = {
                k: row.get(k)
                for k in (
                    "fixtureId",
                    "participant1Id",
                    "participant2Id",
                    "participant1Name",
                    "participant2Name",
                    "tournamentId",
                    "tournamentName",
                    "startTime",
                )
            }
            previous = saved.get(str(row["fixtureId"]), {})
            maps = (
                dict(previous.get("maps", {}))
                if all(previous.get(k) == v for k, v in identity.items())
                else {}
            )
            for number, definition in definitions.items():
                market = book.get("markets", {}).get(definition["market_id"], {})
                if market.get("marketActive") is not True:
                    continue
                prices, changed = {}, {}
                for side, outcome_id in definition["outcomes"].items():
                    player = (
                        market.get("outcomes", {})
                        .get(outcome_id, {})
                        .get("players", {})
                        .get("0", {})
                    )
                    price = player.get("price")
                    if (
                        player.get("active") is True
                        and isinstance(price, (float, int))
                        and not isinstance(price, bool)
                        and math.isfinite(price)
                        and 1 < price <= 1_000_000
                    ):
                        prices[side], changed[side] = price, player.get("changedAt")
                if set(prices) == {"1", "2"} and 0.8 <= sum(1 / v for v in prices.values()) <= 1.5:
                    maps[number] = {
                        "captured_at": captured.isoformat(),
                        "prices": prices,
                        "changed_at": changed,
                        "market_id": definition["market_id"],
                        "market_name": definition["name"],
                        "url": book.get("fixturePath", ""),
                    }
            if maps:
                saved[str(row["fixtureId"])] = {**identity, "maps": maps}
        self.store.set("fonbet:snapshots", saved)

    def quotes(self, draft, league_names):
        if not draft.map_number:
            return [], "map_unknown"
        names = {normalized(draft.radiant_name), normalized(draft.dire_name)}
        candidates = [
            s
            for s in self.store.get("fonbet:snapshots", {}).values()
            if {
                normalized(s.get("participant1Name", "")),
                normalized(s.get("participant2Name", "")),
            }
            == names
            and league_matches(s, league_names)
            and (start := timestamp(s.get("startTime")))
            and abs((draft.observed_at - start).total_seconds()) <= 12 * 3600
        ]
        if len(candidates) != 1:
            return [], "fonbet_ambiguous" if candidates else "fonbet_unmatched"
        fixture = candidates[0]
        snapshot = fixture.get("maps", {}).get(str(draft.map_number))
        if not snapshot:
            return [], "fonbet_map_missing"
        captured = timestamp(snapshot["captured_at"])
        result = []
        for side in ("1", "2"):
            outcome = (
                "radiant"
                if normalized(fixture[f"participant{side}Name"]) == normalized(draft.radiant_name)
                else "dire"
            )
            result.append(
                Quote(
                    "Fonbet",
                    draft.match_id,
                    "map_winner",
                    outcome,
                    snapshot["prices"][side],
                    captured,
                    captured,
                    draft.map_number,
                    rules=f"oddspapi:fonbet:{fixture['fixtureId']}:{snapshot['market_id']}:prematch",
                    url=snapshot.get("url", ""),
                    market_id=snapshot["market_id"],
                    price_kind="prematch_reference",
                    price_origin="prematch",
                    market_context={
                        "provider": "OddsPapi",
                        "stage": "prematch",
                        "fixture_id": fixture["fixtureId"],
                        "series_start_at": fixture["startTime"],
                        "captured_at": snapshot["captured_at"],
                        "changed_at": snapshot["changed_at"].get(side),
                        "market_name": snapshot["market_name"],
                    },
                )
            )
        if not all(valid_reference(q, draft) for q in result):
            return [], "fonbet_stale"
        return result, "prematch_saved"
