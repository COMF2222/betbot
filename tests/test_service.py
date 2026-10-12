import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.config import Settings
from betbot.domain import utcnow
from betbot.model import History
from betbot.service import Service
from betbot.storage import Store


@pytest.fixture
async def service(tmp_path, prediction):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        model_path=tmp_path / "missing.json",
        admin_ids="1",
        telegram_chat_id=-100,
    )
    store = Store(tmp_path / "ledger.sqlite3")
    store.league(123, "Test", True)
    svc = Service(settings, store)
    svc.model = SimpleNamespace(
        ready=True,
        publication_ready=True,
        reload=lambda: None,
        raw={
            "version": "test-model",
            "history_cutoff": "2020-01-01T00:00:00Z",
            "history": History().export(),
        },
        predict=lambda draft, **kwargs: replace(prediction, draft=draft),
        duration_probability=lambda *args: 0.6,
    )
    svc.collect_quotes = AsyncMock(return_value=[])
    svc.hero_assets = AsyncMock()
    yield svc
    await svc.close()
    store.close()


async def test_disabled_league_and_late_draft_never_create_forecast(service, draft):
    assert await service.process_draft(replace(draft, league_id=999)) is None
    stale = replace(draft, source_updated_at=utcnow() - timedelta(minutes=10))
    assert await service.process_draft(stale) is None
    assert not service.store.forecasts()
    assert service.store.match(draft.match_id)["reason"]


async def test_collect_creates_once_and_default_is_preview(service, draft):
    id_ = await service.process_draft(draft)
    assert id_
    assert await service.process_draft(draft) is None
    assert service.store.forecast(id_)["state"] == "preview"


async def test_missing_quotes_are_added_to_the_same_post_without_changing_the_forecast(
    service, draft
):
    from betbot.domain import Quote

    id_ = await service.process_draft(draft)
    service.bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        edit_message_media=AsyncMock(),
    )
    await service.publish(id_)
    row = service.store.forecast(id_)
    before = row["payload"]
    p = service.saved_prediction(before)
    q = Quote(
        "Polymarket",
        draft.match_id,
        "map_winner",
        p.favorite,
        1.54,
        utcnow(),
        None,
        2,
        price_kind="market",
    )
    await service.attach_missing_quotes(id_, p, [q])
    await service.attach_missing_quotes(id_, p, [q])
    assert service.bot.edit_message_media.await_count == 1
    assert service.bot.edit_message_media.call_args.kwargs["message_id"] == 100
    assert service.store.forecast(id_)["payload"] == before


async def test_morning_brief_runs_at_ten_moscow_and_is_deduplicated(service, monkeypatch):
    from datetime import datetime

    from betbot.presentation import MOSCOW

    service.store.set("auto_publish", True)
    service.store.set("poly_league:123", "BLAST Slam")
    service.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=200))
    )
    service.poly.discover = AsyncMock(
        return_value=[
            {
                "id": "a",
                "title": "Dota 2: A vs B - BLAST Slam Playoffs",
                "startTime": "2026-10-11T09:00:00Z",
                "teams": [{"name": "A"}, {"name": "B"}],
            }
        ]
    )
    monkeypatch.setattr("betbot.service.utcnow", lambda: datetime(2026, 10, 11, 9, tzinfo=MOSCOW))
    await service.morning_brief()
    service.bot.send_message.assert_not_awaited()
    monkeypatch.setattr("betbot.service.utcnow", lambda: datetime(2026, 10, 11, 10, tzinfo=MOSCOW))
    await service.morning_brief()
    await service.morning_brief()
    assert service.bot.send_message.await_count == 1
    assert "12:00 МСК" in service.bot.send_message.call_args.args[1]
    monkeypatch.setattr("betbot.service.utcnow", lambda: datetime(2026, 10, 11, 17, tzinfo=MOSCOW))
    await service.morning_brief()
    assert service.bot.send_message.await_count == 1


async def test_uncertain_remote_send_is_not_retried(service, draft):
    id_ = await service.process_draft(draft)
    service.bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=TimeoutError()), send_message=AsyncMock()
    )
    assert "неизвестен" in await service.publish(id_)
    assert service.store.forecast(id_)["state"] == "uncertain"
    await service.publish(id_)
    assert service.bot.send_photo.await_count == 1


async def test_forecast_then_result_reply_use_original_chat_message_and_topic(service, draft):
    service.store.destination(-100, "Group", "supergroup", 9)
    id_ = await service.process_draft(draft)
    service.bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
    )
    assert "опубликован" in await service.publish(id_)
    service.store.result(draft.match_id, {"radiant_win": True, "duration": 2200})
    service.store.set("chat_id", -200)
    service.store.set("topic_id", 99)
    await service.publish_results()
    last_call = service.bot.send_message.call_args
    assert last_call.args[0] == -100
    assert last_call.kwargs["reply_parameters"].message_id == 100
    assert last_call.kwargs["message_thread_id"] == 9
    await service.publish_results()
    assert service.bot.send_message.await_count == 1
    assert json.loads(service.store.forecast(id_)["payload"])["radiant_probability"] == 0.58


async def test_model_gate_blocks_manual_and_automatic_publication(service, draft):
    service.model.publication_ready = False
    id_ = await service.process_draft(draft)
    service.bot = SimpleNamespace(send_photo=AsyncMock())
    assert "порог" in await service.publish(id_)
    service.bot.send_photo.assert_not_awaited()


async def test_a_failed_old_model_cannot_publish_after_a_new_model_passes(
    service, draft, prediction
):
    service.model.predict = lambda d, **kwargs: replace(
        prediction, draft=d, publication_ready=False
    )
    id_ = await service.process_draft(draft)
    service.bot = SimpleNamespace(send_photo=AsyncMock())
    assert "порог" in await service.publish(id_)
    service.bot.send_photo.assert_not_awaited()


async def test_data_expiring_while_rendering_is_not_sent(service, draft, monkeypatch):
    # Freeze provider checks at valid initial time, then simulate a slow asset request.
    id_ = await service.process_draft(draft)
    service.bot = SimpleNamespace(send_photo=AsyncMock())

    async def slow_assets(_):
        later = utcnow() + timedelta(minutes=10)
        monkeypatch.setattr("betbot.service.utcnow", lambda: later)

    service.hero_assets = slow_assets
    assert "устарели" in await service.publish(id_)
    service.bot.send_photo.assert_not_awaited()


async def test_finished_games_update_history_once_and_survive_restart(service, draft):
    now = utcnow()
    earlier = replace(draft, match_id="100", observed_at=now - timedelta(hours=2))
    service.store.observe(earlier, "observed")
    service.store.result(
        "100",
        {
            "radiant_win": True,
            "duration": 1800,
            "start_time": int((now - timedelta(hours=2)).timestamp()),
            "confirmed_at": (now - timedelta(hours=1)).isoformat(),
        },
    )
    first, as_of = service.online_history(now)
    assert first.games == 1
    again, _ = service.online_history(now)
    assert again.games == 1
    service.live_state = None
    restored, restored_as_of = service.online_history(now)
    assert restored.games == 1 and restored_as_of == as_of


async def test_unfinished_or_not_yet_confirmed_game_does_not_update_history(service, draft):
    now = utcnow()
    service.store.observe(draft, "observed")
    service.store.result(
        draft.match_id,
        {
            "radiant_win": True,
            "duration": 1800,
            "start_time": int((now - timedelta(seconds=1800)).timestamp()),
            "confirmed_at": now.isoformat(),
        },
    )
    history, _ = service.online_history(now)
    assert history.games == 0  # Completion buffer has not elapsed.


async def test_report_image_sent_once_to_bound_group_and_topic(service):
    service.store.destination(-100, "Group", "supergroup", 9)
    service.bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=77)), send_message=AsyncMock()
    )
    await service.send_report("day:test:final", "✅ Итоги дня", b"image")
    await service.send_report("day:test:final", "✅ Итоги дня", b"image")
    service.bot.send_photo.assert_awaited_once()
    call = service.bot.send_photo.call_args
    assert call.args[0] == -100 and call.kwargs["message_thread_id"] == 9
    assert call.kwargs["caption"] == "✅ Итоги дня"
    service.bot.send_message.assert_not_awaited()


async def test_uncertain_report_image_is_not_retried(service):
    service.bot = SimpleNamespace(send_photo=AsyncMock(side_effect=TimeoutError()))
    await service.send_report("day:test:final", "Итоги дня", b"image")
    await service.send_report("day:test:final", "Итоги дня", b"image")
    service.bot.send_photo.assert_awaited_once()


async def test_tournament_cache_preserves_recent_games_if_live_source_fails(service):
    service.store.set("league_activity", {"123": {"last_match_at": utcnow().isoformat()}})
    service.odota.live = AsyncMock(side_effect=TimeoutError())
    service.odota.pro_matches = AsyncMock(return_value=[])
    await service.refresh_league_activity(force=True)
    assert "last_match_at" in service.store.get("league_activity")["123"]
    assert any(
        r["source"] == "Текущие турниры" and not r["ok"] for r in service.store.health_rows()
    )


async def test_forecast_and_result_fan_out_with_separate_post_ids(service, draft):
    service.store.destination(-100, "Group", "supergroup", 9)
    service.store.destination(-200, "Channel", "channel", 99)  # Channel must clear a group topic.
    identity = await service.process_draft(draft)

    async def photo(chat_id, *args, **kwargs):
        return SimpleNamespace(message_id=101 if chat_id == -100 else 201)

    service.bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=photo),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=301)),
    )
    await service.publish(identity)
    assert service.bot.send_photo.await_count == 2
    service.bot.send_message.assert_not_awaited()
    service.store.result(draft.match_id, {"radiant_win": True})
    await service.publish_results()
    replies = service.bot.send_message.call_args_list[-2:]
    assert [(c.args[0], c.kwargs["reply_parameters"].message_id) for c in replies] == [
        (-100, 101),
        (-200, 201),
    ]
    await service.publish(identity)
    await service.publish_results()
    assert service.bot.send_photo.await_count == 2 and service.bot.send_message.await_count == 2
    assert service.store.summary()["published"] == 1


async def test_timeout_in_group_does_not_block_channel_or_cause_duplicate_posts(service, draft):
    service.store.destination(-200, "Channel", "channel")
    identity = await service.process_draft(draft)

    async def photo(chat_id, *args, **kwargs):
        if chat_id == -100:
            raise TimeoutError()
        return SimpleNamespace(message_id=200)

    service.bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=photo),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=201)),
    )
    assert "неизвестен" in await service.publish(identity)
    assert service.store.forecast(identity)["state"] == "published"
    await service.publish(identity)
    assert service.bot.send_photo.await_count == 2
    assert service.store.uncertain()[0]["destination"] == -100
    service.store.result(draft.match_id, {"radiant_win": False})
    await service.publish_results()
    assert service.bot.send_message.call_args.args[0] == -200
    assert service.bot.send_message.call_args.kwargs["reply_parameters"].message_id == 200


async def test_reports_go_to_each_enabled_destination_and_are_not_duplicated(
    service, draft, monkeypatch
):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 10, 20, 30, tzinfo=UTC)
    monkeypatch.setattr("betbot.service.utcnow", lambda: now)
    monkeypatch.setattr("betbot.storage.utcnow", lambda: now)
    monkeypatch.setattr("betbot.reports.utcnow", lambda: now)
    service.store.destination(-200, "Channel", "channel")
    service.store.set("auto_publish", True)
    service.bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=40)))
    # Record deliveries directly: this test checks reporting of immutable posts, not draft timing.
    service.store.observe(draft, "observed")
    identity = service.store.add_forecast(service.model.predict(draft))
    for target in service.destinations:
        delivery = service.store.begin_delivery(identity, "forecast", target["chat_id"])
        service.store.finish_delivery(delivery, "sent", 10)
    service.store.result(draft.match_id, {"radiant_win": True})
    await service.reports()
    assert service.bot.send_photo.await_count == 0  # No preliminary post at 23:30.
    now += timedelta(hours=1)  # The calendar day has closed in Moscow.
    await service.reports()
    await service.reports()
    assert service.bot.send_photo.await_count == 2
    assert {c.args[0] for c in service.bot.send_photo.call_args_list} == {-100, -200}
    assert all("1 из 1" in c.kwargs["caption"] for c in service.bot.send_photo.call_args_list)


async def test_final_report_waits_for_last_result_and_survives_repeated_ticks(
    service, draft, monkeypatch
):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 10, 20, 30, tzinfo=UTC)
    for module in ("service", "storage", "reports"):
        monkeypatch.setattr(f"betbot.{module}.utcnow", lambda: now)
    service.store.set("auto_publish", True)
    service.store.set("daily_report_hour", 23)
    service.bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=40)))
    service.store.observe(draft, "observed")
    identity = service.store.add_forecast(service.model.predict(draft))
    delivery = service.store.begin_delivery(identity, "forecast", -100)
    service.store.finish_delivery(delivery, "sent", 10)
    now += timedelta(hours=1)
    await service.reports()
    assert service.bot.send_photo.await_count == 0
    service.store.result(draft.match_id, {"radiant_win": True})
    await service.reports()
    await service.reports()
    assert service.bot.send_photo.await_count == 1
    assert "предваритель" not in service.bot.send_photo.call_args.kwargs["caption"]


async def test_mixed_timing_stats_share_one_post_without_merging_counts(
    service, draft, monkeypatch
):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 10, 20, 30, tzinfo=UTC)
    for module in ("service", "storage", "reports"):
        monkeypatch.setattr(f"betbot.{module}.utcnow", lambda: now)
    service.store.set("auto_publish", True)
    service.bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=40)))
    for late in (False, True):
        d = replace(draft, match_id="late" if late else draft.match_id)
        p = replace(service.model.predict(d), late_analysis=late)
        service.store.observe(d, "observed")
        identity = service.store.add_forecast(p)
        delivery = service.store.begin_delivery(identity, "forecast", -100)
        service.store.finish_delivery(delivery, "sent", 10)
        service.store.result(d.match_id, {"radiant_win": not late})
    now += timedelta(hours=1)
    await service.reports()
    await service.reports()
    assert service.bot.send_photo.await_count == 1
    body = service.bot.send_photo.call_args.kwargs["caption"]
    assert "1 из 1 · итоги дня" in body and "Отдельный учёт: 0 из 1" in body


@pytest.mark.parametrize("old_late", [False, True])
async def test_existing_final_of_other_timing_kind_blocks_duplicate_after_upgrade(
    service, draft, monkeypatch, old_late
):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 10, 20, 30, tzinfo=UTC)
    for module in ("service", "storage", "reports"):
        monkeypatch.setattr(f"betbot.{module}.utcnow", lambda: now)
    service.store.set("auto_publish", True)
    service.bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=40)))
    p = replace(service.model.predict(draft), late_analysis=not old_late)
    service.store.observe(draft, "observed")
    identity = service.store.add_forecast(p)
    delivery = service.store.begin_delivery(identity, "forecast", -100)
    service.store.finish_delivery(delivery, "sent", 10)
    service.store.result(draft.match_id, {"radiant_win": True})
    key = "day:2026-10-10:final" + (":delayed" if old_late else "")
    service.store.begin_report(key, -100)
    service.store.finish_report(key, -100, "sent", 99)
    now += timedelta(hours=1)
    await service.reports()
    assert service.bot.send_photo.await_count == 0


async def test_legacy_binding_migrates_once_and_does_not_reenable_disabled_target(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        model_path=tmp_path / "missing.json",
        telegram_chat_id=-100,
        telegram_topic_id=9,
    )
    store = Store(tmp_path / "state.db")
    svc = Service(settings, store)
    assert svc.destinations[0]["chat_id"] == -100 and svc.destinations[0]["topic_id"] == 9
    store.toggle_destination(-100)
    await svc.close()
    restored = Service(settings, store)
    assert not restored.destinations and restored.chat_id is None
    await restored.close()
    store.close()


async def test_disabling_channel_during_group_send_prevents_next_send(service, draft):
    service.store.destination(-200, "Channel", "channel")
    identity = await service.process_draft(draft)

    async def photo(chat_id, *args, **kwargs):
        service.store.toggle_destination(-200)
        return SimpleNamespace(message_id=100)

    service.bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=photo),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
    )
    await service.publish(identity)
    service.bot.send_photo.assert_awaited_once()
    assert service.bot.send_photo.call_args.args[0] == -100


async def test_confirmed_ambiguous_forecast_gets_its_result_without_extra_explanation(
    service, draft
):
    identity = await service.process_draft(draft)
    service.bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=TimeoutError()),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
    )
    await service.publish(identity)
    assert service.store.confirm_delivery(identity, "forecast", 100, -100)
    service.bot.send_message.assert_not_awaited()
    service.store.result(draft.match_id, {"radiant_win": True})
    await service.publish_results()
    await service.publish_results()
    service.bot.send_message.assert_awaited_once()
    assert service.bot.send_message.call_args.kwargs["reply_parameters"].message_id == 100


async def test_disabled_channel_gets_no_result_and_can_resume_later(service, draft):
    service.store.destination(-200, "Channel", "channel")
    identity = await service.process_draft(draft)
    service.bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
    )
    await service.publish(identity)
    service.store.result(draft.match_id, {"radiant_win": True})
    service.store.toggle_destination(-200)
    service.bot.send_message.reset_mock()
    await service.publish_results()
    service.bot.send_message.assert_awaited_once()
    assert service.bot.send_message.call_args.args[0] == -100
    service.store.toggle_destination(-200)
    await service.publish_results()
    assert service.bot.send_message.await_count == 2
    assert service.bot.send_message.call_args.args[0] == -200
