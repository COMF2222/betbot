import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.config import Settings
from betbot.domain import Quote, timestamp, utcnow
from betbot.model import History
from betbot.paper_bank import PaperBank
from betbot.quote_wait import deadline, original_pair_ready
from betbot.service import Service
from betbot.storage import Store


@pytest.fixture
async def worker(tmp_path, prediction, monkeypatch):
    clock = SimpleNamespace(now=utcnow())
    for module in (
        "service",
        "quote_wait",
        "storage",
        "timing",
        "paper_bank",
        "profit_lab",
        "presentation",
        "providers",
    ):
        monkeypatch.setattr(f"betbot.{module}.utcnow", lambda: clock.now)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        model_path=tmp_path / "none",
        admin_ids="1",
        telegram_chat_id=-100,
    )
    store = Store(tmp_path / "wait.sqlite3")
    store.league(prediction.draft.league_id, "Test", True)
    service = Service(settings, store)
    service.model = SimpleNamespace(
        ready=True,
        publication_ready=True,
        reload=lambda: None,
        raw={
            "version": "test",
            "history_cutoff": "2020-01-01T00:00:00Z",
            "history": History().export(),
        },
        predict=lambda draft, **kw: replace(prediction, draft=draft),
    )
    service.collect_quotes = AsyncMock(return_value=[])
    service.hero_assets = AsyncMock()
    service.bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        send_message=AsyncMock(),
        edit_message_media=AsyncMock(),
    )
    service.store.set("auto_publish", True)
    yield service, clock
    await service.close()
    store.close()


def prices(draft, now):
    own = Quote(
        "Polymarket",
        draft.match_id,
        "map_winner",
        "radiant",
        1.80,
        now,
        None,
        draft.map_number,
        market_id="exact-map",
        price_kind="market",
        ask_odds=1.75,
    )
    return [own, replace(own, outcome="dire", odds=2.25, ask_odds=2.10)]


async def test_prices_available_later_become_original_only_before_publication(worker, draft):
    service, clock = worker
    bank = PaperBank(service.store)
    bank.start(1, 500000, 10000)
    assert await service.process_draft(draft) is None
    assert service.store.match(draft.match_id)["status"] == "waiting_quotes"
    assert not service.store.forecasts() and not bank.history(1)
    service.bot.send_photo.assert_not_awaited()
    first_deadline = service.store.get(f"quote_wait:{draft.match_id}")["deadline"]
    clock.now += timedelta(seconds=104)
    fresh = replace(draft, observed_at=clock.now, source_updated_at=clock.now, game_time=4)
    service.collect_quotes.return_value = prices(fresh, clock.now)
    identity = await service.process_draft(fresh)
    assert identity and service.store.forecast(identity)["state"] == "published"
    payload = json.loads(service.store.forecast(identity)["payload"])
    assert payload["analysis"]["quote_wait"]["deadline"] == first_deadline
    assert payload["analysis"]["quote_wait"]["outcome"] == "paired_price"
    assert payload["quotes"][0]["ask_odds"] == 1.75
    assert str(bank.history(1)[0]["odds"]) == "1.75"
    assert service.bot.send_photo.await_count == 1
    assert await service.process_draft(fresh) is None


async def test_timeout_publishes_without_inventing_a_price_and_is_not_extended(worker, draft):
    service, clock = worker
    assert await service.process_draft(draft) is None
    end = timestamp(service.store.get(f"quote_wait:{draft.match_id}")["deadline"])
    clock.now += timedelta(seconds=45)
    await service.retry_waiting_quotes(set())
    assert timestamp(service.store.get(f"quote_wait:{draft.match_id}")["deadline"]) == end
    assert not service.store.forecasts()
    clock.now = end
    await service.retry_waiting_quotes(set())
    p = json.loads(service.store.forecasts()[0]["payload"])
    assert not p["quotes"] and p["analysis"]["quote_wait"]["outcome"] == "timeout"
    assert service.bot.send_photo.await_count == 1
    await service.retry_waiting_quotes(set())
    assert service.bot.send_photo.await_count == 1


async def test_queue_does_not_block_other_maps_and_survives_store_restart(worker, draft):
    service, clock = worker
    await service.process_draft(draft)
    before = service.store.get(f"quote_wait:{draft.match_id}")
    restored = Store(service.settings.data_dir / "wait.sqlite3")
    original = service.store
    service.store = restored
    try:
        assert restored.get(f"quote_wait:{draft.match_id}") == before
        other = replace(draft, match_id="another-map", map_number=3)
        service.collect_quotes.return_value = prices(other, clock.now)
        assert await service.process_draft(other)
        assert len(restored.waiting_quote_drafts()) == 1
    finally:
        service.store = original
        restored.close()


async def test_ready_pair_skips_wait_and_single_side_does_not(worker, draft):
    service, clock = worker
    service.collect_quotes.return_value = prices(draft, clock.now)[:1]
    assert await service.process_draft(draft) is None
    assert len(service.store.quote_snapshots(draft.match_id)) == 1
    other = replace(draft, match_id="pair-ready")
    service.collect_quotes.return_value = prices(other, clock.now)
    assert await service.process_draft(other)
    assert not service.store.get(f"quote_wait:{other.match_id}")


@pytest.mark.parametrize(
    "patch",
    [
        {"ask_odds": None},
        {"ask_odds": float("nan")},
        {"map_number": 99},
        {"match_id": "wrong"},
        {"price_origin": "history"},
        {"price_kind": "offer"},
    ],
)
def test_wait_requires_exact_fresh_live_purchase_pair(prediction, patch):
    now = utcnow()
    qs = prices(prediction.draft, now)
    p = replace(prediction, quotes=[replace(qs[0], **patch), qs[1]])
    assert not original_pair_ready(p, 30, now)
    assert original_pair_ready(replace(prediction, quotes=qs), 30, now)


async def test_wait_respects_early_window_and_reclassifies_after_slow_fetch(worker, draft):
    service, clock = worker
    near_start = replace(draft, observed_at=clock.now, source_updated_at=clock.now, game_time=60)
    await service.process_draft(near_start)
    state = service.store.get(f"quote_wait:{draft.match_id}")
    assert (timestamp(state["deadline"]) - clock.now).total_seconds() == 15
    assert service.quote_wait_timeout() == 15
    clock.now += timedelta(seconds=16)

    async def slow_quotes(draft):
        clock.now += timedelta(seconds=20)
        return prices(draft, clock.now)

    service.collect_quotes.side_effect = slow_quotes
    await service.retry_waiting_quotes(set())
    p = json.loads(service.store.forecasts()[0]["payload"])
    assert p["timing_status"] == "late" and p["late_analysis"]


async def test_ended_or_disabled_match_never_falls_out_of_queue_as_new_bet(worker, draft):
    service, clock = worker
    await service.process_draft(draft)
    service.store.result(draft.match_id, {"radiant_win": True})
    clock.now += timedelta(seconds=150)
    await service.retry_waiting_quotes(set())
    assert not service.store.forecasts()
    another = replace(
        draft, match_id="disabled", observed_at=clock.now, source_updated_at=clock.now
    )
    await service.process_draft(another)
    service.store.league(draft.league_id, "Test", False)
    await service.retry_waiting_quotes(set())
    assert not service.store.forecasts()


@pytest.mark.parametrize("mode", ["manual", "disabled", "paused", "zero"])
async def test_manual_preview_or_disabled_market_has_no_quote_wait(worker, draft, mode):
    service, _ = worker
    if mode == "manual":
        service.store.set("auto_publish", False)
    elif mode == "disabled":
        service.store.set("source:Polymarket", False)
    elif mode == "paused":
        service.store.set("paused", True)
    else:
        service.settings.quote_wait_seconds = 0
    assert await service.process_draft(draft)
    assert not service.store.get(f"quote_wait:{draft.match_id}")


def test_wait_deadline_is_bounded_by_original_freshness(draft, tmp_path):
    now = utcnow()
    settings = Settings(_env_file=None, data_dir=tmp_path, quote_wait_seconds=300)
    old = replace(
        draft, observed_at=now - timedelta(seconds=160), source_updated_at=now, clock_known=False
    )
    assert (deadline(old, now, settings) - now).total_seconds() == 5
    old_source = replace(
        draft, observed_at=now, source_updated_at=now - timedelta(seconds=160), clock_known=False
    )
    assert (deadline(old_source, now, settings) - now).total_seconds() == 5


async def test_partial_original_pair_can_be_completed_for_display_without_bank_backfill(
    worker, draft
):
    service, clock = worker
    service.settings.quote_wait_seconds = 0
    service.collect_quotes.return_value = prices(draft, clock.now)[:1]
    identity = await service.process_draft(draft)
    original = service.store.forecast(identity)["payload"]
    clock.now += timedelta(seconds=4)
    service.collect_quotes.return_value = prices(draft, clock.now)
    await service.track_market_prices()
    service.bot.edit_message_media.assert_awaited_once()
    assert service.store.forecast(identity)["payload"] == original
    assert len(service.store.get(f"quote_attached:{identity}:-100")) == 2


def test_report_midnight_caption_names_the_closed_day_not_the_sending_date():
    from betbot.reports import demo_report, report_caption

    data = demo_report()
    data.update(date="2026-10-15", demo=False)
    assert "итоги дня · 15.10</b>" in report_caption(data)


async def test_fonbet_is_fallback_after_wait_and_polymarket_still_has_priority(worker, draft):
    service, clock = worker
    captured = clock.now - timedelta(minutes=10)
    context = {
        "provider": "OddsPapi",
        "stage": "prematch",
        "fixture_id": "fixture",
        "captured_at": captured.isoformat(),
        "series_start_at": (captured + timedelta(minutes=5)).isoformat(),
    }
    fonbet = [
        replace(
            q,
            source="Fonbet",
            observed_at=captured,
            source_updated_at=captured,
            price_origin="prematch",
            price_kind="prematch_reference",
            market_context=context,
        )
        for q in prices(draft, captured)
    ]
    service.collect_quotes.return_value = fonbet
    assert await service.process_draft(draft) is None
    clock.now = timestamp(service.store.get(f"quote_wait:{draft.match_id}")["deadline"])
    await service.retry_waiting_quotes(set())
    assert (
        json.loads(service.store.forecasts()[0]["payload"])["analysis"]["initial_odds_source"]
        == "Fonbet"
    )
    other = replace(
        draft, match_id="polymarket-first", observed_at=clock.now, source_updated_at=clock.now
    )
    service.collect_quotes.return_value = [
        replace(q, match_id=other.match_id) for q in fonbet
    ] + prices(other, clock.now)
    identity = await service.process_draft(other)
    assert (
        json.loads(service.store.forecast(identity)["payload"])["analysis"]["initial_odds_source"]
        == "Polymarket"
    )


async def test_cached_discovery_miss_is_refreshed_inside_wait_window(worker, draft):
    service, clock = worker
    await service.process_draft(draft)
    clock.now += timedelta(seconds=45)
    service.poly.loaded_at = clock.now - timedelta(seconds=45)
    service.poly.discover = AsyncMock(return_value=[])
    await Service.collect_quotes(service, draft)
    service.poly.discover.assert_awaited_once_with(force=True)
    service.poly.loaded_at = clock.now
    await Service.collect_quotes(service, draft)
    assert service.poly.discover.call_args.kwargs == {}


async def test_stale_waiting_draft_and_seen_in_live_are_not_published_twice(worker, draft):
    service, clock = worker
    await service.process_draft(draft)
    await service.retry_waiting_quotes({draft.match_id})
    assert service.collect_quotes.await_count == 1
    clock.now += timedelta(seconds=181)
    await service.retry_waiting_quotes(set())
    assert not service.store.forecasts()
    assert service.store.match(draft.match_id)["status"] == "skipped"
