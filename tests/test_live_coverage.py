import copy
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.domain import Draft, utcnow
from betbot.presentation import caption
from betbot.providers import (
    ProviderError,
    Steam,
    eligible_analysis,
    eligible_draft,
    merge_live_drafts,
    parse_live,
    parse_steam_live,
)


def steam_game(draft, duration=0):
    game = {
        "match_id": int(draft.match_id),
        "league_id": draft.league_id,
        "radiant_team": {"team_id": draft.radiant_id, "team_name": draft.radiant_name},
        "dire_team": {"team_id": draft.dire_id, "team_name": draft.dire_name},
        "radiant_series_wins": 0,
        "dire_series_wins": 0,
        "stream_delay_s": 900,
        "players": [{"account_id": 999, "hero_id": 0, "name": "Observer", "team": 4}],
        "scoreboard": {"duration": duration},
    }
    for number, side in enumerate(("radiant", "dire")):
        players = getattr(draft, side)
        game["players"].extend(
            {"account_id": p.account_id, "hero_id": p.hero_id, "name": p.name, "team": number}
            for p in players
        )
        game["scoreboard"][side] = {
            "score": 99,
            "players": [
                {
                    "account_id": p.account_id,
                    "hero_id": p.hero_id,
                    "player_slot": i + 128 * number,
                    "gold_per_min": 9999,
                    "kills": 90,
                    "net_worth": 99999,
                }
                for i, p in enumerate(players)
            ],
        }
    return game


def test_steam_draft_ignores_observers_and_all_current_performance(draft):
    raw = steam_game(draft)
    original = copy.deepcopy(raw)
    parsed = parse_steam_live(raw)
    assert parsed.complete and parsed.map_number == 1
    assert parsed.radiant == draft.radiant and parsed.dire == draft.dire
    assert raw == original
    data = json.dumps(parsed.to_dict())
    assert all(field not in data for field in ("kills", "gold_per_min", "net_worth", "score"))
    assert Draft.from_dict(parsed.to_dict()) == parsed
    assert eligible_draft(parsed, 180, 90)
    assert eligible_analysis(parsed, 180) is None


def test_top_live_current_clock_does_not_add_broadcast_delay(draft):
    raw = {
        "match_id": draft.match_id,
        "league_id": draft.league_id,
        "team_id_radiant": draft.radiant_id,
        "team_id_dire": draft.dire_id,
        "team_name_radiant": draft.radiant_name,
        "team_name_dire": draft.dire_name,
        "game_time": -50,
        "delay": 900,
        "last_update_time": int(utcnow().timestamp()),
        "players": [
            {"team": team, "team_slot": i, "account_id": p.account_id, "hero_id": p.hero_id}
            for team, side in enumerate(("radiant", "dire"))
            for i, p in enumerate(getattr(draft, side))
        ],
    }
    parsed = parse_live(raw)
    assert eligible_draft(parsed, 180, 90) is None
    assert not parsed.clock_is_delayed
    assert eligible_analysis(parse_live({**raw, "deactivate_time": 1}), 180)


def test_merge_uses_fresh_matching_clock_and_rejects_conflicting_teams(draft):
    steam = parse_steam_live(steam_game(draft))
    top = replace(draft, delay_seconds=900, clock_is_delayed=False)
    merged = merge_live_drafts([top], [steam])[0]
    assert merged.map_number == 1 and merged.series_id == draft.series_id
    assert not merged.clock_is_delayed and eligible_draft(merged, 180, 90) is None
    conflicting = replace(top, dire_id=999)
    assert not merge_live_drafts([conflicting], [steam])[0].active
    stale = replace(top, source_updated_at=utcnow() - timedelta(hours=1))
    assert merge_live_drafts([stale], [steam])[0].source_name == "Steam"


async def test_steam_league_games_keep_working_when_top_endpoint_fails(draft):
    async def get(url, params):
        if "GetTopLiveGame" in url:
            raise ProviderError("Unavailable")
        return {"result": {"games": [steam_game(draft)]}}

    provider = Steam(SimpleNamespace(get=get), "test-key")
    result = await provider.live()
    assert len(result) == 1 and result[0].complete


def test_missing_clock_cannot_be_classified_as_an_early_forecast(draft):
    raw = steam_game(draft)
    del raw["scoreboard"]["duration"]
    parsed = parse_steam_live(raw)
    assert not parsed.clock_known
    assert "неизвестно" in eligible_draft(parsed, 180, 90)
    assert eligible_analysis(parsed, 180) is None


@pytest.mark.parametrize(
    "wins,number", [((0, 0), 1), ((1, 0), 2), ((1, 1), 3), ((2, 1), 4), ((2, 2), 5)]
)
def test_each_played_map_in_bo3_or_bo5_has_its_own_number(draft, wins, number):
    raw = steam_game(draft)
    raw["radiant_series_wins"], raw["dire_series_wins"] = wins
    assert parse_steam_live(raw).map_number == number


@pytest.fixture
async def service(tmp_path, prediction):
    from betbot.config import Settings
    from betbot.model import History
    from betbot.service import Service
    from betbot.storage import Store

    settings = Settings(
        _env_file=None, data_dir=tmp_path, model_path=tmp_path / "none", telegram_chat_id=-100
    )
    store = Store(tmp_path / "state.db")
    store.league(123, "DreamLeague", True)
    svc = Service(settings, store)
    svc.model = SimpleNamespace(
        ready=True,
        publication_ready=True,
        reload=lambda: None,
        raw={
            "version": "test",
            "history_cutoff": "2020-01-01T00:00:00Z",
            "history": History().export(),
        },
        predict=lambda draft, **kwargs: replace(prediction, draft=draft, radiant_probability=0.52),
    )
    svc.collect_quotes = AsyncMock(return_value=[])
    svc.hero_assets = AsyncMock()
    yield svc
    await svc.close()
    store.close()


async def test_low_confidence_delayed_analysis_is_published_once_to_both_destinations(
    service, draft
):
    service.store.destination(-200, "Channel", "channel")
    service.bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)),
    )
    service.store.set("auto_publish", True)
    late = replace(draft, game_time=300, delay_seconds=900)
    identity = await service.process_draft(late)
    assert identity and await service.process_draft(late) is None
    payload = json.loads(service.store.forecast(identity)["payload"])
    assert payload["late_analysis"] and not payload["signal"]
    assert service.bot.send_photo.await_count == 2
    for call in service.bot.send_photo.call_args_list:
        assert (
            "Поздний разбор" in call.kwargs["caption"]
            and "Нет выраженного" in call.kwargs["caption"]
        )
    service.store.result(draft.match_id, {"radiant_win": True})
    stats = service.store.summary()
    assert stats["published"] == 0 and stats["late"]["published"] == stats["late"]["wins"] == 1


async def test_modes_can_still_filter_weak_or_delayed_cards(service, draft):
    service.store.set("publish_all_maps", False)
    assert await service.process_draft(draft) is None
    assert "фаворита" in service.store.match(draft.match_id)["reason"]
    service.store.set("publish_all_maps", True)
    service.store.set("allow_delayed_analysis", False)
    assert await service.process_draft(replace(draft, game_time=300)) is None
    assert "опоздал" in service.store.match(draft.match_id)["reason"]


async def test_same_series_has_independent_forecasts_and_results_per_map(service, draft):
    first = replace(draft, match_id="200", map_number=1)
    second = replace(draft, match_id="201", map_number=2)
    identities = [await service.process_draft(first), await service.process_draft(second)]
    assert all(identities) and identities[0] != identities[1]
    service.store.result("200", {"radiant_win": True})
    assert service.store.forecast(identities[0])["result"]
    assert not service.store.forecast(identities[1])["result"]


async def test_steam_keeps_coverage_when_opendota_is_down(service, draft):
    service.odota.live = AsyncMock(side_effect=TimeoutError())
    service.steam = SimpleNamespace(live=AsyncMock(return_value=[draft]))
    assert await service.live_drafts(force=True) == [draft]
    health = {r["source"]: r["ok"] for r in service.store.health_rows()}
    assert not health["OpenDota live"] and health["Steam live"]


async def test_one_missing_result_does_not_block_others_and_steam_can_settle(service, draft):
    first, second = replace(draft, match_id="100"), replace(draft, match_id="101")
    service.store.observe(first, "observed")
    service.store.observe(second, "observed")
    service.tick_count = 6
    service.odota.pro_matches = AsyncMock(return_value=[])
    service.odota.match = AsyncMock(
        side_effect=[ProviderError("HTTP 404"), {"match_id": 101, "radiant_win": False}]
    )
    service.steam = SimpleNamespace(
        match=AsyncMock(return_value={"match_id": 100, "radiant_win": True})
    )
    await service.collect_results()
    assert json.loads(service.store.match("100")["result"])["source"] == "Steam"
    assert json.loads(service.store.match("101")["result"])["source"] == "OpenDota"


async def test_results_can_be_collected_when_recent_matches_endpoint_is_down(service, draft):
    service.store.observe(draft, "observed")
    service.tick_count = 6
    service.odota.pro_matches = AsyncMock(side_effect=TimeoutError())
    service.odota.match = AsyncMock(side_effect=ProviderError("HTTP 404"))
    service.steam = SimpleNamespace(
        match=AsyncMock(return_value={"match_id": int(draft.match_id), "radiant_win": True})
    )
    await service.collect_results()
    assert json.loads(service.store.match(draft.match_id)["result"])["source"] == "Steam"


async def test_delayed_finished_games_are_not_published(service, draft):
    service.steam = SimpleNamespace(
        match=AsyncMock(return_value={"match_id": int(draft.match_id), "radiant_win": False})
    )
    assert await service.process_draft(replace(draft, game_time=2000)) is None
    assert not service.store.has_forecast(draft.match_id)
    assert service.store.match(draft.match_id)["status"] == "finished"


def test_caption_marks_weak_predictions(prediction):
    assert "Нет выраженного" in caption(replace(prediction, signal=False))
