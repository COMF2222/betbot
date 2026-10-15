import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from betbot.config import Settings
from betbot.domain import Quote, utcnow
from betbot.paper_bank import PaperBank
from betbot.providers import OpenDota, ProviderError, Steam
from betbot.service import Service
from betbot.storage import Store


@pytest.fixture
async def results_service(tmp_path):
    store = Store(tmp_path / "results.sqlite3")
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        model_path=tmp_path / "none.json",
        admin_ids="1",
        telegram_chat_id=-100,
    )
    service = Service(settings, store)
    service.odota.pro_matches = AsyncMock(return_value=[])
    service.odota.match = AsyncMock(return_value={})
    service.steam = None
    service.complete_price_history = AsyncMock()
    service.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=999))
    )
    yield service
    await service.close()
    store.close()


def published(service, prediction, match_id):
    p = replace(prediction, draft=replace(prediction.draft, match_id=match_id))
    p.quotes = [
        Quote(
            "Polymarket",
            match_id,
            "map_winner",
            p.favorite,
            1.8,
            utcnow(),
            None,
            p.draft.map_number,
            price_kind="market",
            gross=True,
            ask_odds=1.8,
        )
    ]
    service.store.observe(p.draft, "observed")
    identity = service.store.add_forecast(p)
    delivery = service.store.begin_delivery(identity, "forecast", -100)
    service.store.finish_delivery(delivery, "sent", int(match_id))
    return p


async def test_completed_maps_settle_bank_and_reply_once_when_opendota_is_down(
    results_service, prediction
):
    service = results_service
    bank = PaperBank(service.store)
    bank.start(1, 100000, 10000)
    first = published(service, prediction, "100")
    second = published(service, prediction, "101")
    payloads = [row["payload"] for row in service.store.forecasts()]
    await service.paper_bets()
    assert bank.summary(1)["counts"] == {"open": 2}
    service.odota.pro_matches.side_effect = ProviderError("HTTP 429: api.opendota.com")
    service.odota.match.side_effect = ProviderError("HTTP 429: api.opendota.com")

    async def result(match_id, league_id):
        assert league_id == prediction.draft.league_id
        return {
            "match_id": int(match_id),
            "radiant_win": first.favorite == "radiant"
            if match_id == "100"
            else second.favorite != "radiant",
            "result_method": "GetMatchHistoryBySequenceNum",
            "match_seq_num": int(match_id) + 10,
        }

    service.steam = SimpleNamespace(match=AsyncMock(side_effect=result))
    service.tick_count = 1  # No six-cycle delay before direct checks.
    await service.collect_results()
    await service.paper_bets()
    assert bank.summary(1)["counts"] == {"won": 1, "lost": 1}
    assert bank.summary(1)["cash_cents"] == 98000
    calls = service.bot.send_message.call_args_list
    public = [c for c in calls if c.args[0] == -100]
    private = [c for c in calls if c.args[0] == 1]
    assert len(public) == 2 and len(private) == 4
    assert {c.kwargs["reply_parameters"].message_id for c in public} == {100, 101}
    await service.collect_results()
    await service.paper_bets()
    assert service.bot.send_message.await_count == 6
    assert [row["payload"] for row in service.store.forecasts()] == payloads


async def test_failed_result_delivery_retries_even_when_no_unresolved_maps_remain(
    results_service, prediction
):
    service = results_service
    p = published(service, prediction, "100")
    service.store.result(p.draft.match_id, {"radiant_win": True})
    service.bot.send_message.side_effect = TelegramBadRequest(
        SendMessage(chat_id=-100, text="test"), "retry"
    )
    await service.collect_results()
    assert not service.store.unresolved()
    assert (
        service.store.db.execute("SELECT state FROM deliveries WHERE kind='result'").fetchone()[0]
        == "failed"
    )
    service.bot.send_message.side_effect = None
    await service.collect_results()
    await service.collect_results()
    assert service.bot.send_message.await_count == 2
    service.odota.pro_matches.assert_not_awaited()


async def test_old_unavailable_maps_cannot_starve_new_map_checks_after_restart(
    results_service, draft, tmp_path
):
    service = results_service
    now = utcnow()
    for i in range(20):
        service.store.observe(replace(draft, match_id=str(100 + i)), "observed")
    service.odota.match = AsyncMock(return_value={})
    await service.collect_results()
    first = {call.args[0] for call in service.odota.match.call_args_list}
    assert len(first) == 8
    service.odota.match.reset_mock()
    await service.collect_results()
    second = {call.args[0] for call in service.odota.match.call_args_list}
    assert len(second) == 8 and not first.intersection(second)
    restored = Store(tmp_path / "results.sqlite3")
    try:
        remaining = restored.result_candidates(now=now + timedelta(seconds=1))
        assert len(remaining) == 4 and not set(remaining).intersection(first | second)
        assert set(restored.result_candidates(limit=20, now=now + timedelta(minutes=2))) == set(
            restored.unresolved()
        )
    finally:
        restored.close()


def test_result_queue_has_no_hundred_map_cutoff(results_service, draft):
    store = results_service.store
    for i in range(105):
        store.observe(replace(draft, match_id=str(1000 + i)), "observed")
    assert len(store.unresolved()) == 105
    checked = set()
    for _ in range(14):
        for match_id in store.result_candidates():
            checked.add(match_id)
            store.result_checked(match_id)
    assert checked == set(store.unresolved())


async def test_result_health_does_not_report_success_when_all_requests_fail(results_service, draft):
    service = results_service
    service.store.observe(draft, "observed")
    service.odota.pro_matches.side_effect = ProviderError("HTTP 429: api.opendota.com")
    service.odota.match.side_effect = ProviderError("HTTP 500: api.opendota.com")
    service.steam = SimpleNamespace(
        match=AsyncMock(side_effect=ProviderError("HTTP 500: api.steampowered.com"))
    )
    await service.collect_results()
    health = {r["source"]: r for r in service.store.health_rows()}
    assert not health["Результаты"]["ok"]
    assert "очередь сохранена" in health["Результаты"]["detail"]
    assert not service.store.match(draft.match_id)["result"]


async def test_recent_result_keeps_postgame_context_and_avoids_direct_duplicate_check(
    results_service, draft
):
    service = results_service
    service.store.observe(draft, "observed")
    result = {"match_id": int(draft.match_id), "radiant_win": False}
    service.odota.pro_matches.return_value = [result]
    service.odota.match.return_value = {
        **result,
        "players": [{"account_id": 123, "player_slot": 0, "gold_per_min": 500}],
    }
    await service.collect_results()
    saved = json.loads(service.store.match(draft.match_id)["result"])
    assert saved["context"]["accounts"] == [123]
    service.odota.match.assert_awaited_once_with(draft.match_id)


async def test_steam_result_uses_league_sequence_and_caches_league_history():
    history = {
        "result": {
            "status": 1,
            "matches": [
                {"match_id": 100, "match_seq_num": 200},
                {"match_id": 101, "match_seq_num": 201},
            ],
        }
    }

    async def get(url, params):
        if "GetMatchHistory/v1" in url:
            assert params["league_id"] == 123
            return history
        assert "GetMatchHistoryBySequenceNum/v1" in url
        return {
            "result": {
                "matches": [
                    {
                        "match_id": params["start_at_match_seq_num"] - 100,
                        "match_seq_num": params["start_at_match_seq_num"],
                        "leagueid": 123,
                        "radiant_win": False,
                    }
                ]
            }
        }

    http = SimpleNamespace(get=AsyncMock(side_effect=get))
    steam = Steam(http, "private-key")
    assert (await steam.match("100", 123))["radiant_win"] is False
    assert (await steam.match("101", 123))["match_id"] == 101
    assert http.get.await_count == 3
    assert all("GetMatchDetails" not in call.args[0] for call in http.get.call_args_list)


@pytest.mark.parametrize(
    "changes", [{"match_id": 999}, {"leagueid": 999}, {"match_seq_num": 999}, {"radiant_win": 1}]
)
async def test_sequence_result_rejects_neighboring_match_wrong_league_or_nonboolean_outcome(
    changes,
):
    row = {"match_id": 100, "match_seq_num": 200, "leagueid": 123, "radiant_win": True}
    http = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                {"result": {"status": 1, "matches": [{"match_id": 100, "match_seq_num": 200}]}},
                {"result": {"matches": [{**row, **changes}]}},
            ]
        )
    )
    assert await Steam(http, "private-key").match("100", 123) == {}


async def test_old_match_can_be_found_beyond_latest_history_page():
    http = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                {"result": {"status": 1, "matches": [{"match_id": 500, "match_seq_num": 600}]}},
                {"result": {"status": 1, "matches": [{"match_id": 100, "match_seq_num": 200}]}},
                {
                    "result": {
                        "matches": [
                            {
                                "match_id": 100,
                                "match_seq_num": 200,
                                "leagueid": 123,
                                "radiant_win": True,
                            }
                        ]
                    }
                },
            ]
        )
    )
    assert (await Steam(http, "private-key").match("100", 123))["radiant_win"]
    assert http.get.call_args_list[1].args[1]["start_at_match_id"] == "100"


async def test_live_missing_match_never_uses_previous_finished_match():
    http = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "result": {"status": 1, "matches": [{"match_id": 100, "match_seq_num": 200}]}
            }
        )
    )
    assert await Steam(http, "private-key").match("101", 123) == {}
    assert http.get.await_count == 1


async def test_opendota_429_cooldown_covers_live_and_result_requests(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("betbot.providers.time.monotonic", lambda: clock[0])
    http = SimpleNamespace(get=AsyncMock(side_effect=ProviderError("HTTP 429: api.opendota.com")))
    odota = OpenDota(http)
    with pytest.raises(ProviderError):
        await odota.pro_matches()
    with pytest.raises(ProviderError):
        await odota.live()
    with pytest.raises(ProviderError):
        await odota.match("100")
    assert http.get.await_count == 1
    clock[0] += 301
    with pytest.raises(ProviderError):
        await odota.pro_matches()
    assert http.get.await_count == 2
