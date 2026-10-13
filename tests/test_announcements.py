from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.announcements import match_time, page_details, page_matches, tournament_caption
from betbot.brief import brief_caption, combined_schedule
from betbot.config import Settings
from betbot.service import Service
from betbot.storage import Store
from betbot.tournaments import Liquipedia

NOW = datetime(2026, 10, 13, 7, tzinfo=UTC)
CONTENT = """{{Infobox league
|name=Example Cup
|tickername=Example CQ
|leagueid=777
|liquipediatier=2
|sdate=2026-10-13
|edate=2026-10-17
|team_number=4
|prizepoolusd=250,000
|organizer=ESL Gaming
|type=Online
|country=Europe
}}
{{TeamParticipants
|{{Opponent|Team A|qualification={{Qualification|method=invite}}}}
|{{Opponent|Team B|qualification={{Qualification|method=invite}}}}
|{{Opponent|tbd|qualification={{Qualification|method=qual}}}}
|{{Opponent||qualification={{Qualification|method=qual}}}}
}}
{{Match
|opponent1={{TeamOpponent|Team A|score=0}}
|opponent2={{TeamOpponent|Team B|score=0}}
|date=October 13, 2026 - 11:00 {{Abbr/CEST}}
|map1={{Map|winner=1}}
|map2={{Map|winner=2}}
|map3={{Map|winner=}}
}}
"""


def test_metadata_uses_confirmed_participants_and_prize_without_nested_contamination():
    event = page_details(CONTENT, "Example/2026", NOW)
    assert event["participants"] == ["Team A", "Team B"]
    assert event["team_count"] == 4 and event["qualifier_slots"] == 2
    assert event["prize_usd"] == 250000 and event["league_id"] == 777
    caption = tournament_caption(event)
    assert "уже 2 команды" in caption and "$250 000" in caption
    assert "Ещё 2 места через квалы" in caption
    matches = page_matches(CONTENT, event)
    assert len(matches) == 1 and not matches[0]["finished"]
    assert matches[0]["best_of"] == 3
    assert matches[0]["teams"] == ["Team A", "Team B"]


def test_match_dates_convert_explicit_timezones_and_reject_missing_zone():
    assert match_time("October 13, 2026 - 11:00 {{Abbr/CEST}}").hour == 12
    assert match_time("October 13, 2026 - 11:00 {{Abbr/CST}}").hour == 6
    assert match_time("October 13, 2026 - 11:00") is None
    assert match_time("October 32, 2026 - 11:00 {{Abbr/CEST}}") is None


async def test_selected_pages_verify_real_ticket_and_reject_old_season():
    provider = Liquipedia(SimpleNamespace())
    provider.query = AsyncMock(
        side_effect=[
            {"query": {"search": [{"title": "Correct"}, {"title": "Wrong"}, {"title": "Old"}]}},
            {
                "query": {
                    "pages": [
                        {"title": name, "revisions": [{"slots": {"main": {"content": content}}}]}
                        for name, content in [
                            ("Correct", CONTENT),
                            ("Wrong", CONTENT.replace("leagueid=777", "leagueid=778")),
                            ("Old", CONTENT.replace("2026-10", "2025-10")),
                        ]
                    ]
                }
            },
        ]
    )
    data = await provider.selected_details([{"id": 777}], NOW)
    assert [e["page"] for e in data["events"]] == ["Correct"]
    assert len(data["matches"]) == 1


def test_schedule_attaches_market_once_and_filters_disabled_finished_and_other_day():
    event = page_details(CONTENT, "Example", NOW)
    wiki = page_matches(CONTENT, event)
    market = [{**wiki[0], "start": "2026-10-13T09:05:00Z", "market": True, "source": "Polymarket"}]
    rows = combined_schedule(
        wiki
        + [
            {**wiki[0], "league_id": 999},
            {**wiki[0], "finished": True},
            {**wiki[0], "start": "2026-10-14T09:00:00Z"},
        ],
        market,
        {777},
        NOW,
    )
    assert len(rows) == 1 and rows[0]["market"]
    assert rows[0]["start"].hour == 12
    assert "первая в 12:00 МСК" in brief_caption(rows)


def test_schedule_does_not_claim_late_match_is_live_without_evidence():
    event = page_details(CONTENT, "Example", NOW)
    wiki = page_matches(CONTENT, event)
    later = datetime(2026, 10, 13, 10, tzinfo=UTC)
    assert not combined_schedule(wiki, [], {777}, later)
    live = combined_schedule([], [{**wiki[0], "live": True}], {777}, later)
    assert len(live) == 1 and "уже идёт" in brief_caption(live)


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setattr("betbot.service.utcnow", lambda: NOW)
    store = Store(tmp_path / "ledger.sqlite3")
    store.league(777, "Example Cup", True)
    store.destination(-100, "Channel", "channel")
    store.set("destinations_migrated", True)
    svc = Service(
        Settings(_env_file=None, data_dir=tmp_path, model_path=tmp_path / "missing.json"), store
    )
    svc.bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=77)), send_message=AsyncMock()
    )
    store.set("auto_publish", True)
    event = page_details(CONTENT, "Example/2026", NOW)
    store.set(
        "publication_catalog",
        {"events": [event], "matches": page_matches(CONTENT, event), "checked_at": NOW.isoformat()},
    )
    yield svc
    await svc.close()
    store.close()


async def test_tournament_card_is_public_for_selected_ticket_once_only(service):
    await service.tournament_announcements()
    await service.tournament_announcements()
    assert service.bot.send_photo.await_count == 1
    call = service.bot.send_photo.call_args
    assert call.args[0] == -100 and "$250 000" in call.kwargs["caption"]
    service.bot.send_message.assert_not_awaited()


async def test_shared_ticket_does_not_send_all_qualifier_regions_at_once(service):
    catalog = service.store.get("publication_catalog")
    catalog["events"].append(
        {
            **catalog["events"][0],
            "source_key": "liquipedia:Example/OtherRegion",
            "name": "Other region",
        }
    )
    service.store.set("publication_catalog", catalog)
    await service.tournament_announcements()
    await service.tournament_announcements()
    assert service.bot.send_photo.await_count == 1


async def test_disabled_stale_or_paused_tournament_is_not_announced(service):
    service.store.league(777, "Example", False)
    await service.tournament_announcements()
    service.bot.send_photo.assert_not_awaited()
    service.store.league(777, "Example", True)
    service.store.set("paused", True)
    await service.tournament_announcements()
    service.bot.send_photo.assert_not_awaited()
    service.store.set("paused", False)
    service.store.set("tournament_announcements_enabled", False)
    await service.tournament_announcements()
    service.bot.send_photo.assert_not_awaited()


async def test_unknown_announcement_send_is_not_repeated(service):
    service.bot.send_photo.side_effect = TimeoutError()
    await service.tournament_announcements()
    await service.tournament_announcements()
    assert service.bot.send_photo.await_count == 1
    assert (
        service.store.report_state("tournament:liquipedia:Example/2026:announcement", -100)
        == "uncertain"
    )


async def test_wiki_schedule_still_posts_when_polymarket_is_unavailable(service):
    service.poly.discover = AsyncMock(side_effect=TimeoutError())
    await service.morning_brief()
    await service.morning_brief()
    assert service.bot.send_photo.await_count == 1
    assert "12:00 МСК" in service.bot.send_photo.call_args.kwargs["caption"]
