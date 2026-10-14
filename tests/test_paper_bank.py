import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.config import Settings
from betbot.domain import Quote, utcnow
from betbot.paper_bank import PaperBank, initial_quote, notification_text, parse_money
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
                **quote_changes,
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
