import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.config import Settings
from betbot.domain import Quote, utcnow
from betbot.paper_bank import (
    PaperBank,
    initial_quote,
    notification_text,
    parse_bands,
    parse_money,
    validate_bands,
)
from betbot.service import Service
from betbot.storage import Store


@pytest.fixture
def ledger(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    yield store
    store.close()


def forecast(store, prediction, suffix="1", odds=1.79, destinations=(-100, -200), **quote_changes):
    p = replace(
        prediction,
        draft=replace(prediction.draft, match_id="123456789" + suffix, observed_at=utcnow()),
        league_name="Example Cup",
    )
    p.quotes = (
        []
        if odds is None
        else [
            Quote(
                "Polymarket",
                p.draft.match_id,
                "map_winner",
                p.favorite,
                odds,
                utcnow(),
                None,
                p.draft.map_number,
                price_kind="market",
                gross=True,
                **{"ask_odds": odds, **quote_changes},
            )
        ]
    )
    store.observe(p.draft, "observed")
    identity = store.add_forecast(p)
    for destination in destinations:
        delivery = store.begin_delivery(identity, "forecast", destination)
        store.finish_delivery(delivery, "sent", 1)
    return identity, p


def test_flat_stake_wins_losses_reservations_and_restart_are_exact(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, parse_money("10000"), parse_money("100"))
    first, a = forecast(ledger, prediction)
    second, b = forecast(ledger, prediction, "2", odds=2.34)
    original = ledger.forecast(first)["payload"]
    bank.capture({1})
    bank.capture({1})
    state = bank.summary(1)
    assert state["cash_cents"] == 980000 and state["reserved"] == 20000
    assert state["equity"] == 1000000 and state["profit"] == 0
    assert state["counts"] == {"open": 2}
    ledger.result(a.draft.match_id, {"radiant_win": a.favorite == "radiant"})
    ledger.result(b.draft.match_id, {"radiant_win": b.favorite != "radiant"})
    bank.settle({1})
    bank.settle({1})
    state = bank.summary(1)
    assert state["cash_cents"] == state["equity"] == 997900
    assert state["profit"] == -2100 and state["reserved"] == 0
    assert state["counts"] == {"won": 1, "lost": 1}
    assert ledger.forecast(first)["payload"] == original
    assert len(bank.history(1)) == 2
    assert bank.history(1)[-1]["odds"] == "1.79"


def test_history_boundary_and_known_outcome_never_create_retrospective_wins(ledger, prediction):
    forecast(ledger, prediction)
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    _, p = forecast(ledger, prediction, "2")
    ledger.result(p.draft.match_id, {"radiant_win": True})
    bank.capture({1})
    assert len(bank.history(1)) == 1
    assert bank.history(1)[0]["reason"] == "already_finished"
    assert bank.summary(1)["cash_cents"] == 100000


def test_missing_original_quote_never_uses_later_attached_or_history_price(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    identity, p = forecast(ledger, prediction, odds=None)
    ledger.set(f"quote_attached:{identity}:-100", [{"odds": 5.0}])
    ledger.save_quotes(
        p.draft.match_id,
        [Quote("Polymarket", p.draft.match_id, "map_winner", p.favorite, 8.5, utcnow(), None, 1)],
    )
    bank.capture({1})
    assert bank.history(1)[0]["reason"] == "no_initial_odds"
    assert bank.summary(1)["cash_cents"] == 100000


def test_missing_map_reason_is_frozen_with_forecast_not_later_source_status(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    p = replace(
        prediction,
        analysis={**prediction.analysis, "market_status": {"code": "map_market_missing"}},
    )
    identity, p = forecast(ledger, p, odds=None)
    ledger.set(f"market_status:{p.draft.match_id}", {"code": "available"})
    bank.capture({1})
    bet = bank.history(1)[0]
    assert bet["reason"] == "map_market_missing" and bet["state"] == "skipped"
    assert "нет отдельного рынка победителя этой карты" in notification_text(
        bank.notifications({1})[0]
    )
    assert ledger.forecast(identity)["payload"] == json.dumps(p.to_dict(), ensure_ascii=False)


def test_bank_uses_first_buy_price_instead_of_more_optimistic_book_reference(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    _, p = forecast(ledger, prediction, odds=1.83, ask_odds=1.03, price_basis="midpoint_fallback")
    bank.capture({1})
    bet = bank.history(1)[0]
    assert bet["odds"] == "1.03" and bet["reason"] == "below_min_odds"
    assert "кэф ниже 1.30" in notification_text(bank.notifications({1})[0])
    assert "ориентир по стакану" not in notification_text(bank.notifications({1})[0])
    ledger.save_quotes(p.draft.match_id, [replace(p.quotes[0], ask_odds=3.0)])
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    assert bank.summary(1)["profit"] == 0
    assert bank.summary(1)["cash_cents"] == 100000


@pytest.mark.parametrize("ask", [None, float("nan"), float("inf"), 1, 0, 2_000_000])
def test_missing_or_invalid_buy_price_never_falls_back_to_market_reference(ledger, prediction, ask):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    forecast(ledger, prediction, ask_odds=ask)
    bank.capture({1})
    assert bank.history(1)[0]["reason"] == "no_initial_buy_price"
    assert bank.summary(1)["cash_cents"] == 100000


@pytest.mark.parametrize(
    "changes",
    [
        {"map_number": 9},
        {"match_id": "other"},
        {"market": "series_winner"},
        {"price_origin": "history"},
        {"price_kind": "offer"},
        {"observed_at": utcnow() + timedelta(minutes=5)},
        {"observed_at": utcnow() - timedelta(minutes=5)},
        {"odds": float("nan")},
        {"odds": float("inf")},
    ],
)
def test_wrong_map_future_stale_or_history_quotes_are_rejected(prediction, changes):
    quote = Quote(
        "Polymarket",
        prediction.draft.match_id,
        "map_winner",
        prediction.favorite,
        1.79,
        utcnow(),
        None,
        prediction.draft.map_number,
        price_kind="market",
        gross=True,
    )
    p = replace(prediction, quotes=[replace(quote, **changes)])
    assert initial_quote(p, utcnow()) is None


def test_first_printed_odds_and_original_stake_stay_frozen(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    _, p = forecast(ledger, prediction, odds=1.785714285)
    bank.capture({1})
    bank.change_stake(1, 25000)
    ledger.save_quotes(p.draft.match_id, [replace(p.quotes[0], odds=4.0)])
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    bet = bank.history(1)[0]
    assert bet["odds"] == "1.79" and bet["stake_cents"] == 10000
    assert bank.summary(1)["profit"] == 7900


def test_no_overdraft_and_pause_does_not_catch_up_paused_forecasts(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 10000, 10000)
    _, p = forecast(ledger, prediction)
    forecast(ledger, prediction, "2")
    bank.capture({1})
    assert bank.summary(1)["cash_cents"] == 0
    assert bank.history(1)[0]["reason"] == "insufficient"
    bank.toggle(1, "active")
    forecast(ledger, prediction, "3")
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    bank.toggle(1, "active")
    bank.capture({1})
    assert len(bank.history(1)) == 2
    assert bank.summary(1)["cash_cents"] == 17900


def test_known_payout_is_recovered_before_new_stake_after_interruption(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 10000, 10000)
    _, first = forecast(ledger, prediction)
    bank.capture({1})
    assert bank.summary(1)["cash_cents"] == 0
    ledger.result(first.draft.match_id, {"radiant_win": first.favorite == "radiant"})
    forecast(ledger, prediction, "2")
    bank.capture({1})
    assert bank.summary(1)["counts"] == {"won": 1, "open": 1}
    assert bank.summary(1)["cash_cents"] == 7900


def test_sessions_are_private_and_new_bank_does_not_take_old_payout(ledger, prediction):
    bank = PaperBank(ledger)
    old = bank.start(1, 100000, 10000)
    _, p = forecast(ledger, prediction)
    bank.capture({1})
    new = bank.start(1, 500000, 20000)
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    assert bank.summary(1, old)["equity"] == 107900
    assert bank.summary(1, new)["equity"] == 500000
    assert bank.summary(2, old) is None and not bank.history(2, old)
    assert bank.notifications({2}) == []


def test_notifications_freeze_balances_and_do_not_repeat_after_ambiguous_send(
    ledger, prediction, tmp_path
):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    _, p = forecast(ledger, prediction)
    bank.capture({1})
    note = bank.notifications({1})[0]
    assert "1.79" in notification_text(note) and "900,00" in notification_text(note)
    assert bank.begin_notification(note["id"])
    ledger.db.commit()
    restored = Store(tmp_path / "paper.sqlite3")
    try:
        assert not PaperBank(restored).notifications({1})
    finally:
        restored.close()
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    result = bank.notifications({1})[0]
    assert "+79,00" in notification_text(result)
    assert json.loads(note["payload"])["cash"] == 90000


def test_disabling_notifications_discards_queue_but_still_updates_balance(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    _, p = forecast(ledger, prediction)
    bank.capture({1})
    bank.toggle(1, "notifications")
    assert not bank.notifications({1})
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    bank.toggle(1, "notifications")
    assert not bank.notifications({1})
    assert bank.summary(1)["profit"] == 7900


def test_rejected_notification_waits_before_retrying(ledger, prediction, monkeypatch):
    now = utcnow()
    monkeypatch.setattr("betbot.paper_bank.utcnow", lambda: now)
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    forecast(ledger, prediction)
    bank.capture({1})
    note = bank.notifications({1})[0]
    assert bank.begin_notification(note["id"])
    bank.finish_notification(note["id"], "planned", retry_after=60)
    assert not bank.notifications({1})
    now += timedelta(seconds=61)
    assert [row["id"] for row in bank.notifications({1})] == [note["id"]]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "Infinity", "1.001", "1000000001", "1e3"])
def test_invalid_amounts_are_rejected(value):
    with pytest.raises(ValueError):
        parse_money(value)


async def test_service_paper_messages_go_only_to_account_owner(ledger, prediction, tmp_path):
    service = Service(
        Settings(
            _env_file=None, data_dir=tmp_path, model_path=tmp_path / "missing.json", admin_ids="1,2"
        ),
        ledger,
    )
    service.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=99))
    )
    try:
        bank = PaperBank(ledger)
        bank.start(1, 100000, 10000)
        _, p = forecast(ledger, prediction)
        await service.paper_bets()
        ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
        await service.paper_bets()
        await service.paper_bets()
        calls = service.bot.send_message.call_args_list
        assert len(calls) == 2 and all(call.args[0] == 1 for call in calls)
    finally:
        await service.close()


@pytest.mark.parametrize(
    ("odds", "stake", "reason"),
    [
        (1.29, 0, "below_min_odds"),
        (1.30, 10000, ""),
        (1.49, 10000, ""),
        (1.50, 15000, ""),
        (1.99, 15000, ""),
        (2.00, 25050, ""),
        (3.20, 25050, ""),
    ],
)
def test_odds_bands_reserve_and_settle_exact_amounts(ledger, prediction, odds, stake, reason):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    bank.set_bands(1, parse_bands("1.30 100\n1.50 150\n2.00 250,50"))
    _, p = forecast(ledger, prediction, odds=odds)
    bank.capture({1})
    bank.capture({1})
    bet = bank.history(1)[0]
    assert bet["stake_cents"] == stake and bet["reason"] == reason
    assert bank.summary(1)["cash_cents"] == 100000 - stake
    assert bank.summary(1)["reserved"] == stake
    assert bank.summary(1)["equity"] == 100000
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    bank.settle({1})
    assert bank.summary(1)["profit"] == bet["stake_cents"] * (round(odds * 100) - 100) // 100


def test_disabled_and_uncovered_ranges_never_fall_back_to_another_stake(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    bank.set_bands(1, parse_bands("1.50 100; 2.00 0; 3.00 200"))
    forecast(ledger, prediction, "1", odds=1.40)
    forecast(ledger, prediction, "2", odds=2.20)
    forecast(ledger, prediction, "3", odds=3.0)
    bank.capture({1})
    rows = list(reversed(bank.history(1)))
    assert [row["reason"] for row in rows] == ["no_stake_band", "stake_disabled", ""]
    assert [row["stake_cents"] for row in rows] == [0, 0, 20000]
    assert bank.summary(1)["cash_cents"] == 80000


def test_insufficient_balance_does_not_reduce_the_selected_band_stake(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 10000, 1000)
    bank.set_bands(1, parse_bands("1.30 10\n2.00 150"))
    forecast(ledger, prediction, odds=2.0)
    bank.capture({1})
    assert bank.history(1)[0]["reason"] == "insufficient"
    assert bank.summary(1)["cash_cents"] == 10000


def test_band_selection_and_payout_use_the_same_frozen_two_decimal_odds(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    bank.set_bands(1, parse_bands("1.30 100\n1.50 150,25"))
    _, p = forecast(ledger, prediction, odds=1.499)
    bank.capture({1})
    bet = bank.history(1)[0]
    assert bet["odds"] == "1.50" and bet["stake_cents"] == 15025
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    assert bank.history(1)[0]["payout_cents"] == 22538
    assert bank.summary(1)["profit"] == 7513


def test_versioned_rules_preserve_queued_forecasts_and_survive_restart(
    ledger, prediction, tmp_path
):
    bank = PaperBank(ledger)
    identity = bank.start(1, 100000, 10000)
    old_version = bank.rules(1)["id"]
    first, p = forecast(ledger, prediction, "1", odds=2.0)
    bank.set_bands(1, parse_bands("1.30 50\n2.00 250"))
    second, _ = forecast(ledger, prediction, "2", odds=2.0)
    assert bank.rules(1, identity, first)["id"] == old_version
    assert bank.rules(1, identity, second)["id"] != old_version
    restored = Store(tmp_path / "paper.sqlite3")
    try:
        resumed = PaperBank(restored)
        resumed.capture({1})
        rows = list(reversed(resumed.history(1)))
        assert [row["stake_cents"] for row in rows] == [10000, 25000]
        assert json.loads(rows[0]["snapshot"])["stake_rules"]["id"] == old_version
        snapshots = [row["snapshot"] for row in rows]
        resumed.change_stake(1, 5000)
        assert [row["snapshot"] for row in reversed(resumed.history(1))] == snapshots
        restored.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
        resumed.settle({1})
        assert resumed.summary(1)["cash_cents"] == 85000
        assert resumed.summary(1)["reserved"] == 25000
    finally:
        restored.close()


def test_legacy_bank_keeps_queued_stake_when_first_rule_version_is_saved(ledger, prediction):
    bank = PaperBank(ledger)
    identity = bank.start(1, 100000, 10000)
    # A pre-upgrade bank has no rule rows; its original flat amount remains authoritative.
    with ledger.db:
        ledger.db.execute("DELETE FROM paper_rule_versions WHERE session_id=?", (identity,))
    assert bank.rules(1)["id"] == 0
    assert [b["stake_cents"] for b in bank.rules(1)["bands"]] == [10000] * 3
    forecast(ledger, prediction, "1")
    bank.change_stake(1, 25000, identity, expected_version=0)
    forecast(ledger, prediction, "2")
    bank.capture({1})
    assert [r["stake_cents"] for r in reversed(bank.history(1))] == [10000, 25000]


def test_old_open_bet_below_new_minimum_still_settles_on_its_original_terms(ledger, prediction):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    _, p = forecast(ledger, prediction)
    bank.capture({1})
    # Reproduce an existing pre-upgrade open bet without invoking the new capture rules.
    with ledger.db:
        ledger.db.execute("UPDATE paper_bets SET odds='1.03'")
    before = bank.history(1)[0]
    bank.set_bands(1, parse_bands("1.30 0"))
    ledger.result(p.draft.match_id, {"radiant_win": p.favorite == "radiant"})
    bank.settle({1})
    after = bank.history(1)[0]
    assert after["snapshot"] == before["snapshot"]
    assert after["odds"] == "1.03" and after["stake_cents"] == 10000
    assert after["payout_cents"] == 10300
    assert bank.summary(1)["profit"] == 300


def test_rule_edits_are_owner_scoped_and_reject_stale_or_archived_sessions(ledger):
    bank = PaperBank(ledger)
    old = bank.start(1, 100000, 10000)
    version = bank.rules(1)["id"]
    other = bank.start(2, 200000, 20000)
    assert bank.rules(2, old) is None
    with pytest.raises(ValueError):
        bank.set_bands(2, parse_bands("1.30 50"), session_id=old)
    bank.set_bands(1, parse_bands("1.30 50"), old, version)
    with pytest.raises(ValueError):
        bank.set_bands(1, parse_bands("1.30 200"), old, version)
    current = bank.start(1, 300000, 30000)
    with pytest.raises(ValueError):
        bank.set_bands(1, parse_bands("1.30 200"), old)
    assert bank.rules(1, old)["bands"] == parse_bands("1.30 50")
    assert bank.rules(1, current)["bands"] == parse_bands("1.30 300\n1.50 300\n2.00 300")
    assert bank.summary(2, other)["cash_cents"] == 200000


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1.29 100",
        "1.30 -1",
        "1.30 nan",
        "1.30 1e3",
        "1.30 1.001",
        "1.30 100\n1.30 200",
        "2.00 100\n1.50 200",
        "1.30 100 extra",
        "inf 100",
        "1.301 100",
        "1000001 100",
        "1.30 1000000001",
        "\n".join(f"{i + 2} 100" for i in range(9)),
    ],
)
def test_invalid_band_tables_are_rejected_without_mutating_rules(ledger, text):
    bank = PaperBank(ledger)
    bank.start(1, 100000, 10000)
    before = bank.rules(1)
    with pytest.raises(ValueError):
        bank.set_bands(1, parse_bands(text))
    assert bank.rules(1) == before


def test_band_parser_accepts_decimal_commas_and_zero_and_rejects_noninteger_cents():
    assert parse_bands("1,3 0\n1,50 150,25; 2 200") == [
        {"min_odds": "1.30", "stake_cents": 0},
        {"min_odds": "1.50", "stake_cents": 15025},
        {"min_odds": "2.00", "stake_cents": 20000},
    ]
    for amount in (True, 100.5, "100", -1):
        with pytest.raises(ValueError):
            validate_bands([{"min_odds": "1.30", "stake_cents": amount}])
