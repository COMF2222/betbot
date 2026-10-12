from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.config import Settings
from betbot.domain import Quote, utcnow
from betbot.price_alerts import favorite_quote
from betbot.service import Service
from betbot.storage import Store


@pytest.fixture
async def service(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        model_path=tmp_path / "missing.json",
        telegram_chat_id=-100,
    )
    store = Store(tmp_path / "ledger.sqlite3")
    store.league(123, "Test", True)
    svc = Service(settings, store)
    yield svc
    await svc.close()
    store.close()


def pair(p, now, odds=1.8, **changes):
    own = Quote(
        "Polymarket",
        p.draft.match_id,
        "map_winner",
        p.favorite,
        odds,
        now,
        None,
        p.draft.map_number,
        rules="same-map",
        market_id="m",
        price_kind="market",
        **changes,
    )
    return [
        own,
        replace(
            own, outcome="dire" if p.favorite == "radiant" else "radiant", odds=1 / (1 - 1 / odds)
        ),
    ]


def published(service, prediction, now):
    p = replace(prediction, draft=replace(prediction.draft, observed_at=now - timedelta(minutes=5)))
    p.quotes = pair(p, p.draft.observed_at, 1.5)
    service.store.observe(p.draft, "observed")
    identity = service.store.add_forecast(p)
    for target in service.destinations:
        delivery = service.store.begin_delivery(
            identity, "forecast", target["chat_id"], target["topic_id"]
        )
        service.store.finish_delivery(delivery, "sent", 101 if target["chat_id"] == -100 else 201)
    service.store.set("auto_publish", True)
    service.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=301))
    )
    return identity, p


async def test_alerts_reply_to_each_original_post_with_cooldown_and_map_limit(
    service, prediction, monkeypatch
):
    service.store.destination(-200, "Channel", "channel")
    now = utcnow()
    monkeypatch.setattr("betbot.service.utcnow", lambda: now)
    identity, p = published(service, prediction, now)
    before = service.store.forecast(identity)["payload"]
    await service.notify_price_increase(identity, p, pair(p, now))
    assert service.bot.send_message.await_count == 2
    calls = service.bot.send_message.call_args_list
    assert [(c.args[0], c.kwargs["reply_parameters"].message_id) for c in calls] == [
        (-100, 101),
        (-200, 201),
    ]
    assert "1.50 → 1.80" in calls[0].args[1] and "🏆" in calls[0].args[1]
    assert "Исходный прогноз: 58%" in calls[0].args[1]
    now += timedelta(minutes=5)
    await service.notify_price_increase(identity, p, pair(p, now, 2.4))
    assert service.bot.send_message.await_count == 2
    for odds in (2.3, 2.8, 10):
        now += timedelta(minutes=11)
        await service.notify_price_increase(identity, p, pair(p, now, odds))
    assert service.bot.send_message.await_count == 6
    assert service.store.forecast(identity)["payload"] == before


@pytest.mark.parametrize(
    "mode",
    [
        "small",
        "stale",
        "history",
        "other_market",
        "settled",
        "disabled",
        "paused",
        "source_off",
        "only_one",
    ],
)
async def test_no_alert_for_invalid_or_ineligible_price_moves(service, prediction, mode):
    now = utcnow()
    identity, p = published(service, prediction, now)
    quotes = pair(p, now, 2.0)
    if mode == "small":
        quotes = pair(p, now, 1.6)
    elif mode == "stale":
        quotes = pair(p, now - timedelta(hours=1), 2)
    elif mode == "history":
        quotes = pair(p, now, 2, price_origin="history")
    elif mode == "other_market":
        quotes = [replace(q, market_id="other") for q in quotes]
    elif mode == "settled":
        service.store.result(p.draft.match_id, {"radiant_win": True})
    elif mode == "disabled":
        service.store.set("odds_alerts_enabled", False)
    elif mode == "paused":
        service.store.set("paused", True)
    elif mode == "source_off":
        service.store.set("source:Polymarket", False)
    elif mode == "only_one":
        quotes = quotes[:1]
    await service.notify_price_increase(identity, p, quotes)
    service.bot.send_message.assert_not_awaited()


async def test_uncertain_send_is_not_duplicated_after_restart_state(
    service, prediction, monkeypatch
):
    now = utcnow()
    monkeypatch.setattr("betbot.service.utcnow", lambda: now)
    identity, p = published(service, prediction, now)
    service.bot.send_message.side_effect = TimeoutError()
    await service.notify_price_increase(identity, p, pair(p, now, 1.9))
    now += timedelta(minutes=11)
    await service.notify_price_increase(identity, p, pair(p, now, 1.9))
    service.bot.send_message.assert_awaited_once()
    assert service.store.uncertain()[0]["kind"] == "odds_alert:1"


async def test_tracking_connects_live_quotes_to_notifications(service, prediction):
    now = utcnow()
    identity, p = published(service, prediction, now)
    service.collect_quotes = AsyncMock(return_value=pair(p, now, 1.9))
    await service.track_market_prices()
    service.bot.send_message.assert_awaited_once()
    assert service.store.quote_snapshots(p.draft.match_id)


def test_terminal_or_inconsistent_pairs_never_trigger_alerts(prediction):
    now = utcnow()
    assert favorite_quote(prediction, pair(prediction, now, 100), 30, now) is None
    quotes = pair(prediction, now)
    assert favorite_quote(prediction, [replace(q, odds=1.01) for q in quotes], 30, now) is None
