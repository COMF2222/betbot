from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from betbot import admin_ui
from betbot.config import Settings
from betbot.profit_lab import POLICIES, PROTOCOL_HASH, ProfitLab
from betbot.profit_review import ProfitReview, checkpoint, progress_text
from betbot.service import Service
from betbot.storage import Store


def entries(count=100, *, clusters=30, timing="early", outcome=True):
    return [
        {
            "forecast_id": index + 1,
            "cluster": str(index % clusters),
            "timing": timing,
            "radiant_win": outcome,
            "decisions": {
                policy: {"take": True, "side": "radiant", "odds": 2.0} for policy in POLICIES
            },
        }
        for index in range(count)
    ]


@pytest.fixture
def store(tmp_path):
    ledger = Store(tmp_path / "review.sqlite3")
    yield ledger
    ledger.close()


@pytest.fixture
async def service(store, tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        model_path=tmp_path / "missing.json",
        admin_ids="1,-100",
        telegram_chat_id=-200,
    )
    worker = Service(settings, store)
    worker.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=40))
    )
    run = worker.profit_lab.start()
    worker.profit_lab = SimpleNamespace(entries=lambda: (run, entries()))
    yield worker
    await worker.close()


def test_rubric_is_minimum_100_unique_control_maps_and_30_series():
    assert not checkpoint(entries(99))["ready"]
    assert not checkpoint(entries(100, clusters=29))["ready"]
    assert checkpoint(entries())["ready"]
    pending = entries(outcome=None)
    assert checkpoint(pending)["selected"] == 100
    assert checkpoint(pending)["settled"] == 0
    assert not checkpoint(pending)["ready"]


def test_skipped_controls_do_not_count_eight_policies_as_eight_maps():
    sample = entries(120)
    for entry in sample[:21]:
        entry["decisions"]["model_all"]["take"] = False
    state = checkpoint(sample)
    assert state["selected"] == state["settled"] == 99
    assert not state["ready"]
    assert "99/100" in progress_text(sample)


def test_sample_boundary_ignores_wins_and_result_arrival_order():
    sample = entries(150, outcome=None)
    expected = checkpoint(sample)["cutoff_forecast_id"]
    assert expected == 100
    for entry in reversed(sample):
        entry["radiant_win"] = entry["forecast_id"] % 2 == 0
    assert checkpoint(list(reversed(sample)))["cutoff_forecast_id"] == expected
    assert len(checkpoint(sample)["cohort"]) == 100


def test_waits_for_30th_series_without_choosing_a_profitable_stopping_point():
    sample = entries(100, clusters=29)
    sample.extend(entries(1))
    sample[-1].update(forecast_id=101, cluster="new-series")
    assert checkpoint(sample)["cutoff_forecast_id"] == 101
    assert checkpoint(sample)["selected"] == 101


def test_waits_for_other_policy_open_maps_but_not_unselected_or_future_maps():
    sample = entries()
    extra = deepcopy(sample[0])
    extra.update(forecast_id=0, radiant_win=None)
    extra["decisions"]["model_all"]["take"] = False
    sample.append(extra)
    assert checkpoint(sample)["pending_maps"] == 1
    assert not checkpoint(sample)["ready"]
    for pick in extra["decisions"].values():
        pick["take"] = False
    future = deepcopy(sample[0])
    future.update(forecast_id=101, radiant_win=None)
    sample.append(future)
    assert checkpoint(sample)["ready"]
    assert checkpoint(sample)["cutoff_forecast_id"] == 100


def test_report_is_frozen_survives_restart_and_does_not_mutate_bank(store, tmp_path):
    run = ProfitLab(store).start()
    sample = entries()
    for entry in sample:
        entry["decisions"]["strong"] = {"take": False, "reason": "weak_model"}
    review = ProfitReview(store).prepare(run, sample)
    assert review["sample"]["settled"] == 100
    assert review["report"]["policies"][0]["review_candidate"]
    assert "Есть кандидаты" in review["text"]
    assert "нет ставок" in review["text"]
    assert len(review["text"]) < 4096
    assert ProfitReview(store).prepare(run, entries(200, outcome=False)) == review
    restored = Store(tmp_path / "review.sqlite3")
    try:
        assert ProfitReview(restored).saved(run) == review
    finally:
        restored.close()
    assert store.db.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM paper_sessions").fetchone()[0] == 0


def test_delayed_winning_sample_does_not_claim_profitable_early_entry(store):
    review = ProfitReview(store).prepare(
        ProfitLab(store).start(), entries(timing="delayed_or_unverified")
    )
    assert "данных пока недостаточно" in review["text"]
    assert "Есть кандидаты" not in review["text"]
    assert review["sample"]["early_controls"] == 0


def test_negative_early_sample_has_unprofitable_verdict(store):
    review = ProfitReview(store).prepare(ProfitLab(store).start(), entries(outcome=False))
    assert "прибыльная стратегия не подтверждена" in review["text"]
    assert all(row["roi"] == -1 for row in review["report"]["policies"])


async def test_private_notice_once_across_ticks_and_restart(service, tmp_path):
    await service.notify_profit_review()
    await service.notify_profit_review()
    service.bot.send_message.assert_awaited_once()
    call = service.bot.send_message.call_args
    assert call.args[0] == 1
    assert "этап проверки доходности завершён" in call.args[1]
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "ui:profit-lab:review"
    reopened = Store(tmp_path / "review.sqlite3")
    try:
        service.store = reopened
        await service.notify_profit_review()
        service.bot.send_message.assert_awaited_once()
    finally:
        reopened.close()
    # The test does not run Service.tick, so no other outgoing messages are possible.


async def test_notice_not_due_or_without_bot_cannot_send(service):
    run, _ = service.profit_lab.entries()
    service.profit_lab = SimpleNamespace(entries=lambda: (run, entries(99)))
    await service.notify_profit_review()
    service.bot.send_message.assert_not_awaited()
    assert not ProfitReview(service.store).saved(run)
    service.bot = None
    service.profit_lab = SimpleNamespace(entries=lambda: (run, entries()))
    await service.notify_profit_review()
    assert not ProfitReview(service.store).saved(run)


@pytest.mark.parametrize("failure", ["timeout", "restart", "forbidden", "rate_limit"])
async def test_ambiguous_delivery_not_duplicated_and_definite_errors_wait(service, failure):
    run, _ = service.profit_lab.entries()
    key = ProfitReview.key(run)
    if failure == "restart":
        # Store startup converts this persisted in-flight report to uncertain.
        service.store.begin_report(key, 1)
        service.store.finish_report(key, 1, "uncertain")
    else:
        method = SendMessage(chat_id=1, text="test")
        error = (
            TimeoutError()
            if failure == "timeout"
            else TelegramRetryAfter(method=method, message="wait", retry_after=15)
            if failure == "rate_limit"
            else TelegramForbiddenError(method=method, message="blocked")
        )
        service.bot.send_message.side_effect = error
    await service.notify_profit_review()
    await service.notify_profit_review()
    assert service.bot.send_message.await_count == (0 if failure == "restart" else 1)
    state = service.store.report_state(key, 1)
    assert state == ("uncertain" if failure in ("timeout", "restart") else "failed")
    if state == "failed":
        service.store.set(f"{key}:retry:1", "2020-01-01T00:00:00+00:00")
        service.bot.send_message.side_effect = None
        await service.notify_profit_review()
        assert service.store.report_state(key, 1) == "sent"


async def test_broken_research_cannot_block_worker(service):
    service.profit_lab = SimpleNamespace(entries=lambda: (_ for _ in ()).throw(ValueError()))
    await service.notify_profit_review()
    service.bot.send_message.assert_not_awaited()
    assert any(
        row["source"] == "Уведомления об исследовании" and not row["ok"]
        for row in service.store.health_rows()
    )


def test_panel_shows_target_and_preserves_original_run_and_frozen_protocol(store, prediction):
    lab = ProfitLab(store)
    run = lab.start()
    assert "0/100" in admin_ui.profit_lab(SimpleNamespace(store=store)).text
    assert "0/30" in admin_ui.profit_lab(SimpleNamespace(store=store)).text
    saved = ProfitReview(store).prepare(run, entries())
    assert admin_ui.profit_lab(SimpleNamespace(store=store), "review").text == saved["text"]
    assert lab.start() == run
    assert PROTOCOL_HASH == "76659e3ee8b69286a4244adf246362347dd497984c3480b7ccc586eb140b17f2"


async def test_tick_checks_review_after_outcomes_even_with_publication_paused(service):
    order = []
    service.store.set("paused", True)
    service.store.set("tournament_catalog_checked_at", "2999-01-01T00:00:00+00:00")
    service.start_tournament_refresh = lambda: None
    service.model = SimpleNamespace(reload=lambda: None)
    for name in (
        "refresh_league_activity",
        "track_market_prices",
        "morning_brief",
        "reports",
        "notify_tournaments",
    ):
        setattr(service, name, AsyncMock())
    for name in ("collect_results", "paper_bets", "notify_profit_review"):

        async def mark(step=name):
            order.append(step)

        setattr(service, name, mark)
    await service.tick()
    assert order == ["collect_results", "paper_bets", "notify_profit_review"]
