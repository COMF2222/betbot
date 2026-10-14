import copy
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.domain import Quote, utcnow
from betbot.fonbet import FonbetPrematch, map_catalogue, valid_reference
from betbot.paper_bank import PaperBank, initial_quote, initial_source, notification_text
from betbot.presentation import caption, market_snapshot, result_text
from betbot.price_alerts import comparable_quote, favorite_quote
from betbot.providers import OddsPapi, ProviderError
from betbot.storage import Store


def catalogue():
    return [
        {
            "marketId": 1647 + 2 * i,
            "sportId": 16,
            "marketLength": 2,
            "marketName": f"{word} Map Winner (incl. overtime)",
            "marketType": "moneyline",
            "period": f"p{i + 1}",
            "handicap": 0,
            "playerProp": False,
            "outcomes": [
                {"outcomeId": 1648 + 2 * i, "outcomeName": "2"},
                {"outcomeId": 1647 + 2 * i, "outcomeName": "1"},
            ],
        }
        for i, word in enumerate(("First", "Second", "Third", "Fourth", "Fifth"))
    ]


def fixture(draft, captured):
    start = captured + timedelta(minutes=5)
    market = {
        "marketActive": True,
        "outcomes": {
            "1647": {
                "players": {
                    "0": {
                        "active": True,
                        "price": 2.35,
                        "changedAt": (captured - timedelta(hours=2)).isoformat(),
                    }
                }
            },
            "1648": {
                "players": {
                    "0": {
                        "active": True,
                        "price": 1.52,
                        "changedAt": (captured - timedelta(hours=2)).isoformat(),
                    }
                }
            },
        },
    }
    return {
        "fixtureId": "fn-fixture",
        "sportId": 16,
        "statusId": 0,
        "startTime": start.isoformat(),
        "participant1Id": 5,
        "participant2Id": 6,
        "participant1Name": draft.dire_name,
        "participant2Name": draft.radiant_name,
        "tournamentId": 52376,
        "tournamentName": "EPL Masters",
        "bookmakerOdds": {
            "fonbet": {
                "bookmakerIsActive": True,
                "suspended": False,
                "fixturePath": "https://fonbet.com/sports/example",
                "markets": {"1647": market},
            }
        },
    }


class API:
    key = "not-a-real-key"

    def __init__(self, row):
        self.row, self.calls, self.account_calls = row, [], 0
        self.remaining = 250
        self.fail = False

    async def account(self):
        self.account_calls += 1
        return {
            "authenticated": True,
            "dota_allowed": True,
            "fonbet_allowed": True,
            "fonbet_live": False,
            "request_limit": 250,
            "request_count": 250 - self.remaining,
            "remaining": self.remaining,
        }

    async def request(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        if self.fail:
            raise ProviderError("HTTP 403: api.oddspapi.io")
        return catalogue() if endpoint == "markets" else [copy.deepcopy(self.row)]


@pytest.fixture
def saved(tmp_path, prediction):
    store = Store(tmp_path / "fn.sqlite3")
    captured = prediction.draft.observed_at - timedelta(minutes=30)
    row = fixture(prediction.draft, captured)
    api = API(row)
    fn = FonbetPrematch(api, store)
    fn.save_batch([row], map_catalogue(catalogue()), ["EPL Masters"], captured)
    draft = replace(prediction.draft, map_number=1)
    quotes, issue = fn.quotes(draft, ["EPL Masters"])
    p = replace(prediction, draft=draft, quotes=quotes, analysis={"initial_odds_source": "Fonbet"})
    assert issue == "prematch_saved"
    yield store, fn, api, row, p
    store.close()


def test_canonical_outcome_labels_are_used_instead_of_array_order(saved):
    _, _, _, _, p = saved
    pair = {q.outcome: q for q in p.quotes}
    assert pair["dire"].odds == 2.35 and pair["radiant"].odds == 1.52
    assert all(valid_reference(q, p.draft) for q in p.quotes)
    assert all(not q.fresh(utcnow(), 30) for q in p.quotes)
    assert all(
        q.price_kind == "prematch_reference" and q.price_origin == "prematch" for q in p.quotes
    )
    assert all(q.source_updated_at == q.observed_at for q in p.quotes)
    assert pair["dire"].market_context["changed_at"] != pair["dire"].observed_at.isoformat()


@pytest.mark.parametrize(
    "changes",
    [
        {"sportId": 17},
        {"statusId": 1},
        {"statusId": None},
        {"trueStartTime": "2026-01-01T00:00:00Z"},
        {"participant1Name": "Other"},
        {"startTime": "2020-01-01T00:00:00Z"},
    ],
)
def test_live_wrong_sport_unknown_stage_and_past_start_are_never_cached(tmp_path, draft, changes):
    store = Store(tmp_path / "rejected.sqlite3")
    try:
        captured = utcnow()
        row = {**fixture(draft, captured), **changes}
        fn = FonbetPrematch(API(row), store)
        fn.save_batch([row], map_catalogue(catalogue()), ["EPL Masters"], captured)
        if changes == {"participant1Name": "Other"}:
            assert fn.quotes(replace(draft, map_number=1), ["EPL Masters"])[0] == []
        else:
            assert store.get("fonbet:snapshots", {}) == {}
    finally:
        store.close()


@pytest.mark.parametrize("target,value", [("suspended", True), ("bookmakerIsActive", False)])
def test_suspended_book_or_inactive_selection_is_rejected(tmp_path, draft, target, value):
    store = Store(tmp_path / "suspended.sqlite3")
    try:
        captured = utcnow()
        row = fixture(draft, captured)
        row["bookmakerOdds"]["fonbet"][target] = value
        fn = FonbetPrematch(API(row), store)
        fn.save_batch([row], map_catalogue(catalogue()), ["EPL Masters"], captured)
        assert store.get("fonbet:snapshots", {}) == {}
    finally:
        store.close()


def test_missing_map_series_winner_and_ambiguous_fixtures_do_not_supply_prices(saved):
    store, fn, _, row, p = saved
    assert fn.quotes(replace(p.draft, map_number=3), ["EPL Masters"])[1] == "fonbet_map_missing"
    duplicate = copy.deepcopy(store.get("fonbet:snapshots"))
    duplicate["other"] = {**duplicate["fn-fixture"], "fixtureId": "other"}
    store.set("fonbet:snapshots", duplicate)
    assert fn.quotes(p.draft, ["EPL Masters"])[1] == "fonbet_ambiguous"
    assert fn.quotes(p.draft, ["DreamLeague"])[0] == []
    assert (
        fn.quotes(
            replace(p.draft, observed_at=p.draft.observed_at + timedelta(days=3)), ["EPL Masters"]
        )[0]
        == []
    )


@pytest.mark.parametrize(
    "change",
    [
        {"observed_at": utcnow() + timedelta(hours=2)},
        {"price_origin": "live"},
        {"price_kind": "offer"},
        {"source": "Polymarket"},
        {"map_number": 3},
        {"match_id": "other"},
    ],
)
def test_invalid_reference_cannot_enter_the_bank(saved, change):
    _, _, _, _, p = saved
    bad = replace(p.quotes[0], **change)
    assert not valid_reference(bad, p.draft)
    assert initial_quote(replace(p, quotes=[bad]), utcnow()) is None


def test_paper_bet_freezes_first_reference_and_no_current_or_later_price_is_substituted(saved):
    store, fn, _, row, p = saved
    bank = PaperBank(store)
    bank.start(1, 100000, 10000)
    store.observe(p.draft, "observed")
    fid = store.add_forecast(p)
    delivery = store.begin_delivery(fid, "forecast", -100)
    store.finish_delivery(delivery, "sent", 11)
    bank.capture({1})
    bet = bank.history(1)[0]
    before = store.forecast(fid)["payload"]
    own, _, _ = market_snapshot(p)
    assert bet["state"] == "open" and bet["odds"] == f"{own.odds:.2f}"
    assert json.loads(bet["snapshot"])["quote"]["price_kind"] == "prematch_reference"
    row["bookmakerOdds"]["fonbet"]["markets"]["1647"]["outcomes"]["1647"]["players"]["0"][
        "price"
    ] = 2.5
    fn.save_batch(
        [row], map_catalogue(catalogue()), ["EPL Masters"], own.observed_at + timedelta(seconds=1)
    )
    bank.capture({1})
    assert bank.history(1)[0]["odds"] == bet["odds"]
    note = notification_text(bank.notifications({1})[0])
    assert "Fonbet" in note and "МСК" not in note and "до начала" not in note
    store.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    bank.settle({1})
    assert bank.summary(1)["profit"] == round((own.odds - 1) * 10000)
    assert store.forecast(fid)["payload"] == before


def test_public_text_is_compact_and_does_not_pretend_reference_is_fresh(saved):
    _, _, _, _, p = saved
    text = caption(p)
    assert "Fonbet:" in text and "МСК" not in text and "до начала" not in text
    assert "Fonbet" in result_text(p, {"radiant_win": True})


def test_polymarket_is_always_preferred_and_fonbet_is_only_a_saved_fallback(saved):
    _, _, _, _, p = saved
    now = utcnow()
    poly = [
        Quote(
            "Polymarket",
            p.draft.match_id,
            "map_winner",
            side,
            odds,
            now,
            None,
            1,
            price_kind="market",
            ask_odds=ask,
        )
        for side, odds, ask in (("radiant", 1.5, 1.4), ("dire", 3, 2.8))
    ]
    combined = replace(p, quotes=p.quotes + poly)
    assert initial_source(combined, now) == "Polymarket"
    chosen = replace(combined, analysis={"initial_odds_source": "Polymarket"})
    assert initial_quote(chosen, now).source == "Polymarket"
    assert market_snapshot(chosen)[0].source == "Polymarket"
    assert initial_source(p, now) == "Fonbet"
    assert initial_source(replace(p, quotes=[]), now) == "Polymarket"
    assert (
        initial_source(replace(p, quotes=p.quotes + [replace(q, ask_odds=None) for q in poly]), now)
        == "Fonbet"
    )
    assert (
        initial_source(
            replace(p, quotes=[replace(q, observed_at=now + timedelta(minutes=1)) for q in poly]),
            now,
        )
        == "Polymarket"
    )


def test_market_catalogue_never_confuses_series_winner_map_periods_or_duplicate_definitions():
    rows = catalogue()
    series = {**rows[0], "marketId": 161, "marketName": "Winner", "period": "result"}
    wrong = {**rows[0], "period": "p3"}
    assert map_catalogue([series, wrong]) == {}
    assert map_catalogue([rows[0], {**rows[0], "marketId": 99}]) == {}


def test_polymarket_moves_never_use_fonbet_as_a_baseline(saved):
    _, _, _, _, p = saved
    now = p.draft.observed_at
    poly = [
        Quote(
            "Polymarket",
            p.draft.match_id,
            "map_winner",
            side,
            price,
            now,
            None,
            1,
            market_id="poly",
            rules="poly-rules",
            price_kind="market",
            ask_odds=price,
        )
        for side, price in (
            (p.favorite, 1.5),
            ("dire" if p.favorite == "radiant" else "radiant", 3),
        )
    ]
    p = replace(p, quotes=p.quotes + poly)
    q = favorite_quote(p, poly, 30, now)
    assert comparable_quote(p, p.quotes, q).source == "Polymarket"
    assert comparable_quote(p, [v for v in p.quotes if v.source == "Fonbet"], q) is None
    peak = replace(q, odds=2.0, observed_at=now + timedelta(minutes=5))
    body = result_text(
        p,
        {"radiant_win": p.favorite == "radiant", "start_time": now.timestamp(), "duration": 600},
        [{"observed_at": peak.observed_at.isoformat(), "quotes": [peak.to_dict()]}],
    )
    assert "Polymarket: 1.50 → наблюдали до 2.00" in body
    # No Polymarket quote at publication: later reads still form their own baseline.
    initial = replace(p, quotes=[q for q in p.quotes if q.source == "Fonbet"])
    first = now + timedelta(minutes=3)
    snapshots = [
        {
            "observed_at": first.isoformat(),
            "quotes": [replace(q, observed_at=first).to_dict() for q in poly],
        },
        {"observed_at": peak.observed_at.isoformat(), "quotes": [peak.to_dict()]},
    ]
    body = result_text(
        initial,
        {"radiant_win": p.favorite == "radiant", "start_time": now.timestamp(), "duration": 600},
        snapshots,
    )
    assert "Polymarket: 1.50 → наблюдали до 2.00" in body


async def test_batch_prefetch_resumes_after_restart_and_refreshes_once_in_five_minute_window(
    tmp_path, draft, monkeypatch
):
    store = Store(tmp_path / "budget.sqlite3")
    now = utcnow()
    row = fixture(draft, now + timedelta(minutes=55))
    api = API(row)
    try:
        monkeypatch.setattr("betbot.fonbet.utcnow", lambda: now)
        fn = FonbetPrematch(api, store)
        await fn.refresh(["EPL Masters"], now)
        assert [c[0] for c in api.calls] == ["markets", "fixtures", "odds-by-tournaments"]
        assert api.calls[-1][1]["bookmakers"] == "fonbet"
        saved_time = store.get("fonbet:snapshots")["fn-fixture"]["maps"]["1"]["captured_at"]
        assert store.get("fonbet:meter")["used"] == 3
        fn = FonbetPrematch(api, store)
        await fn.refresh(["EPL Masters"], now + timedelta(minutes=54))
        assert len(api.calls) == 3
        later = now + timedelta(minutes=55)
        monkeypatch.setattr("betbot.fonbet.utcnow", lambda: later)
        await fn.refresh(["EPL Masters"], later)
        assert len(api.calls) == 4 and api.calls[-1][0] == "odds-by-tournaments"
        assert store.get("fonbet:snapshots")["fn-fixture"]["maps"]["1"]["captured_at"] != saved_time
        await FonbetPrematch(api, store).refresh(["EPL Masters"], later + timedelta(seconds=45))
        assert len(api.calls) == 4
        assert "not-a-real-key" not in json.dumps(store.get("fonbet:account"))
    finally:
        store.close()


async def test_failed_calls_are_budgeted_and_daily_and_account_limits_survive_restart(
    tmp_path, draft
):
    store = Store(tmp_path / "errors.sqlite3")
    now = utcnow()
    api = API(fixture(draft, now))
    api.fail = True
    try:
        fn = FonbetPrematch(api, store, daily_limit=1)
        await fn.refresh(["EPL Masters"], now)
        assert len(api.calls) == 1 and store.get("fonbet:meter")["used"] == 1
        await FonbetPrematch(api, store, daily_limit=1).refresh(
            ["EPL Masters"], now + timedelta(minutes=16)
        )
        assert len(api.calls) == 1
        api.remaining = 0
        store.set("fonbet:account", {})
        await FonbetPrematch(api, store).refresh(["EPL Masters"], now + timedelta(days=1))
        assert len(api.calls) == 1
    finally:
        store.close()


async def test_account_is_sanitized_without_book_catalogue_or_live_calls():
    http = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "api_key": "must-never-be-returned",
                "subscriptions": [
                    {
                        "is_active": True,
                        "sport_ids": [16],
                        "request_limit": 250,
                        "request_count": 6,
                        "bookmakers": {"fonbet": {"has_live_odds": False}},
                    }
                ],
            }
        )
    )
    result = await OddsPapi(http, "private-test-key", "https://api.oddspapi.io/v4").account()
    assert result["remaining"] == 244 and result["fonbet_allowed"] and not result["fonbet_live"]
    assert "must-never-be-returned" not in json.dumps(result)
    assert "private-test-key" not in json.dumps(result)
    assert http.get.await_args.kwargs["attempts"] == 1


async def test_admin_shows_fonbet_fallback_quota_without_a_new_paid_request(saved):
    from betbot.admin_ui import sources

    store, fn, api, _, _ = saved
    await fn.account()
    screen = sources(
        SimpleNamespace(
            store=store,
            odds=api,
            fonbet=fn,
            settings=SimpleNamespace(oddspapi_daily_request_limit=8),
        )
    )
    assert "запасной" in screen.text.lower() and "250" in screen.text
    assert "live Fonbet недоступен" in screen.text
    assert "not-a-real-key" not in screen.text
    assert api.calls == []


async def test_daily_request_counter_uses_moscow_midnight(saved):
    store, fn, api, _, _ = saved
    fn.daily_limit = 1
    before = utcnow().replace(hour=20, minute=59, second=0, microsecond=0)
    await fn.billed("fixtures", {}, before)
    first_day = store.get("fonbet:meter")["day"]
    await fn.billed("fixtures", {}, before + timedelta(minutes=2))
    assert store.get("fonbet:meter")["day"] != first_day
    assert store.get("fonbet:meter")["used"] == 1 and len(api.calls) == 2
