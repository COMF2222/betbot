from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from betbot.config import Settings
from betbot.service import Service
from betbot.storage import Store
from betbot.tournaments import (
    USER_AGENT,
    Liquipedia,
    calendar_dates,
    enrich_markets,
    event_status,
    infobox_fields,
    parse_calendar,
    tournament_notice_window,
)

NOW = datetime(2026, 10, 12, 8, tzinfo=UTC)
EVENT = {
    "source_key": "liquipedia:Example/2026",
    "page": "Example/2026",
    "name": "Example Cup 2026",
    "tier": 1,
    "start_date": "2026-10-20",
    "end_date": "2026-10-27",
    "league_id": 777,
    "source": "https://liquipedia.net/dota2/Example/2026",
    "tier_source": "Liquipedia",
}


def calendar_row(page, name, dates):
    return f'<tr><td><img src="icon.png"></td><td class="column&#95;&#95;tournament"><a href="/dota2/{page}">{name}</a></td><td>{dates}</td><td>$100,000</td></tr>'


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Oct 20–27, 2026", ("2026-10-20", "2026-10-27")),
        ("Nov 30 - Dec 12, 2026", ("2026-11-30", "2026-12-12")),
        ("Dec 28, 2026 - Jan 3, 2027", ("2026-12-28", "2027-01-03")),
        ("Oct 20, 2026", ("2026-10-20", "2026-10-20")),
    ],
)
def test_calendar_date_ranges(text, expected):
    assert tuple(x.isoformat() for x in calendar_dates(text)) == expected


def test_calendar_parser_filters_archives_and_preserves_page_identity():
    html = (
        "<table>"
        + calendar_row("Example/2026", "Example Cup 2026", "Oct 20–27, 2026")
        + calendar_row("Old/2013", "Old Cup", "Oct 1–9, 2013")
        + calendar_row("Unknown", "Unknown Cup", "TBA")
        + "</table>"
    )
    events = parse_calendar(html, 1, NOW)
    assert len(events) == 1
    assert events[0]["source_key"] == EVENT["source_key"]
    assert events[0]["start_date"] == "2026-10-20"
    assert events[0]["tier_source"] == "Liquipedia"


def test_infobox_ignores_other_events_on_the_same_page():
    content = "{{Infobox league\n|liquipediatier=2\n|leagueid=777\n|sdate=2026-10-20\n|edate=2026-10-27\n|previous={{BASEPAGENAME}}/2\n}}\n{{Other\n|liquipediatier=1\n|leagueid=999\n}}"
    assert infobox_fields(content)["liquipediatier"] == "2"
    assert infobox_fields(content)["leagueid"] == "777"


async def test_own_infobox_tier_overrides_listing_and_unknown_tier_is_rejected(monkeypatch):
    provider = Liquipedia(SimpleNamespace())
    calendar = (
        "<table>"
        + "".join(calendar_row(f"Cup/{i}", f"Cup {i}", "Oct 20–27, 2026") for i in (1, 2, 3))
        + "</table>"
    )
    pages = [
        {
            "title": f"Cup/{i}",
            "revisions": [
                {
                    "slots": {
                        "main": {
                            "content": f"{{{{Infobox league\n|liquipediatier={tier}\n|leagueid={700 + i}\n|sdate=2026-10-20\n|edate=2026-10-27\n}}}}"
                        }
                    }
                }
            ],
        }
        for i, tier in ((1, 1), (2, 3), (3, ""))
    ]
    provider.query = AsyncMock(
        side_effect=[
            {"parse": {"text": {"*": calendar}}},
            {"parse": {"text": {"*": calendar}}},
            {"query": {"pages": pages}},
        ]
    )
    events = await provider.tournaments(NOW)
    assert [(e["name"], e["tier"], e["league_id"]) for e in events] == [("Cup 1", 1, 701)]


def test_polymarket_never_promotes_qualifier_or_replaces_tournament_start_date():
    match = {
        "startTime": "2026-10-22T09:00:00Z",
        "slug": "match",
        "eventMetadata": {"league": "Example Cup", "leagueTier": "1", "serie": "Closed Qualifier"},
    }
    assert enrich_markets([EVENT], [match], NOW)[0].get("polymarket_url") is None
    match["eventMetadata"]["serie"] = "2026"
    enriched = enrich_markets([EVENT], [match], NOW)[0]
    assert enriched["start_date"] == EVENT["start_date"]
    assert enriched["next_match_at"] == "2026-10-22T09:00:00+00:00"
    assert enriched["polymarket_url"].endswith("/match")


def test_event_status_does_not_claim_a_future_match_started():
    assert event_status(EVENT, now=NOW) == "Скоро начнётся"
    assert event_status({**EVENT, "start_date": "2026-10-12"}, now=NOW) == "Начинается сегодня"
    assert event_status({**EVENT, "start_date": "2026-10-11"}, now=NOW) == "Уже начался"


@pytest.fixture
async def tournament_service(tmp_path, monkeypatch):
    monkeypatch.setattr("betbot.service.utcnow", lambda: NOW)
    monkeypatch.setattr("betbot.storage.utcnow", lambda: NOW)
    monkeypatch.setattr("betbot.admin_ui.utcnow", lambda: NOW)
    monkeypatch.setattr("betbot.tournaments.utcnow", lambda: NOW)
    store = Store(tmp_path / "ledger.sqlite3")
    svc = Service(
        Settings(
            _env_file=None, admin_ids="1,2", data_dir=tmp_path, model_path=tmp_path / "missing.json"
        ),
        store,
    )
    svc.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=91)))
    svc.liquipedia.tournaments = AsyncMock(return_value=[EVENT])
    svc.liquipedia.cache_team_logos = AsyncMock(return_value=0)
    svc.liquipedia.selected_details = AsyncMock(
        return_value={"events": [], "matches": [], "checked_at": NOW.isoformat(), "league_ids": []}
    )
    svc.poly.discover = AsyncMock(return_value=[])
    yield svc
    await svc.close()
    store.close()


async def test_notifications_are_admin_only_once_per_admin_and_independent_of_publications(
    tournament_service,
):
    svc = tournament_service
    svc.store.destination(-100, "Public channel", "channel")
    svc.store.set("paused", True)
    svc.store.set("auto_publish", False)
    await svc.refresh_tournaments()
    await svc.notify_tournaments()
    await svc.refresh_tournaments()
    await svc.notify_tournaments()
    calls = svc.bot.send_message.call_args_list
    assert len(calls) == 2
    assert {call.args[0] for call in calls} == {1, 2}
    assert all("20.10.2026" in call.args[1] and "Tier 1" in call.args[1] for call in calls)
    assert len(svc.store.tournaments()) == 1
    svc.store.db.execute("UPDATE tournament_notices SET state='sending' WHERE admin_id=1")
    svc.store.db.commit()
    reopened = Store(svc.settings.data_dir / "ledger.sqlite3")
    assert reopened.tournament_notice(1, 1)["state"] == "uncertain"
    reopened.close()
    await svc.notify_tournaments()
    assert svc.bot.send_message.await_count == 2


async def test_unknown_remote_send_is_not_repeated(tournament_service):
    svc = tournament_service
    svc.bot.send_message.side_effect = TimeoutError()
    await svc.refresh_tournaments()
    await svc.notify_tournaments()
    await svc.notify_tournaments()
    assert svc.bot.send_message.await_count == 2
    assert svc.store.tournament_notice(1, 1)["state"] == "uncertain"


async def test_skip_keeps_catalog_and_watch_is_idempotent(tournament_service):
    svc = tournament_service
    await svc.refresh_tournaments()
    svc.store.decide_tournament(1, 1, "skipped")
    await svc.notify_tournaments()
    assert svc.bot.send_message.await_count == 1
    assert svc.bot.send_message.call_args.args[0] == 2
    assert svc.store.tournament(1)
    svc.store.watch_tournament(1)
    svc.store.watch_tournament(1)
    assert svc.store.enabled(777)
    svc.store.league(777, "Renamed", False)
    await svc.refresh_tournaments()
    assert not svc.store.enabled(777)  # A later refresh respects a manual disable.


async def test_selection_waits_for_actual_league_id_without_guessing(tournament_service):
    svc = tournament_service
    svc.liquipedia.tournaments.return_value = [{**EVENT, "league_id": None}]
    await svc.refresh_tournaments()
    svc.store.watch_tournament(1)
    assert svc.store.tournament(1)["enabled"]
    assert not svc.store.enabled(777)
    svc.liquipedia.tournaments.return_value = [EVENT]
    await svc.refresh_tournaments()
    assert svc.store.enabled(777)
    assert svc.calendar[-1]["league_id"] == 777


async def test_calendar_failure_retains_catalog_and_does_not_repeat_notices(tournament_service):
    svc = tournament_service
    await svc.refresh_tournaments()
    await svc.notify_tournaments()
    svc.liquipedia.tournaments.side_effect = RuntimeError("Unavailable")
    await svc.refresh_tournaments()
    await svc.notify_tournaments()
    assert svc.store.tournament(1)["name"] == EVENT["name"]
    assert svc.bot.send_message.await_count == 2


async def test_removed_or_downgraded_event_is_not_announced_as_tier_one(tournament_service):
    svc = tournament_service
    await svc.refresh_tournaments()
    svc.liquipedia.tournaments.return_value = []
    await svc.refresh_tournaments()
    await svc.notify_tournaments()
    svc.bot.send_message.assert_not_awaited()
    assert svc.store.tournament(1)  # Still available for manual selection.


async def test_refresh_runs_in_background_and_has_persistent_half_hour_cache(tournament_service):
    svc = tournament_service
    task = svc.start_tournament_refresh()
    assert task is not None
    assert svc.start_tournament_refresh() is task
    await task
    assert svc.start_tournament_refresh() is None
    svc.liquipedia.tournaments.assert_awaited_once()
    task = svc.start_tournament_refresh(force=True)
    await task
    assert svc.liquipedia.tournaments.await_count == 2


async def test_liquipedia_rate_limits_parse_requests_and_does_not_retry_http_failure(monkeypatch):
    response = httpx.Response(
        503, request=httpx.Request("GET", "https://liquipedia.net/dota2/api.php")
    )
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    provider = Liquipedia(SimpleNamespace(client=client))
    sleep = AsyncMock()
    monkeypatch.setattr("betbot.tournaments.asyncio.sleep", sleep)
    monkeypatch.setattr("betbot.tournaments.time.monotonic", lambda: 100)
    from betbot.providers import ProviderError

    with pytest.raises(ProviderError):
        await provider.query(action="parse", page="Tier_1_Tournaments")
    client.get.assert_awaited_once()
    with pytest.raises(ProviderError):
        await provider.query(action="parse", page="Tier_2_Tournaments")
    sleep.assert_awaited_once_with(30.1)
    assert client.get.call_args.kwargs["headers"]["User-Agent"] == USER_AGENT


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 12, 8, tzinfo=UTC), ("2026-10-12", "2026-10-31")),
        (datetime(2026, 10, 31, 22, tzinfo=UTC), ("2026-11-01", "2026-11-30")),
        (datetime(2028, 2, 12, 8, tzinfo=UTC), ("2028-02-12", "2028-02-29")),
    ],
)
def test_notice_window_ends_at_calendar_month_end_in_moscow(now, expected):
    assert tournament_notice_window(now) == expected


async def test_alerts_cover_current_month_and_ongoing_events_only(tournament_service, monkeypatch):
    svc = tournament_service
    events = [
        {
            **EVENT,
            "source_key": "liquipedia:Ongoing",
            "name": "Ongoing",
            "start_date": "2026-09-30",
            "end_date": "2026-10-13",
        },
        {
            **EVENT,
            "source_key": "liquipedia:LastDay",
            "name": "LastDay",
            "start_date": "2026-10-31",
            "end_date": "2026-11-02",
        },
        {
            **EVENT,
            "source_key": "liquipedia:November",
            "name": "November",
            "start_date": "2026-11-01",
            "end_date": "2026-11-07",
        },
        {
            **EVENT,
            "source_key": "liquipedia:Old",
            "name": "Old",
            "start_date": "2026-10-01",
            "end_date": "2026-10-11",
        },
    ]
    svc.liquipedia.tournaments.return_value = events
    await svc.refresh_tournaments()
    await svc.notify_tournaments()
    calls = svc.bot.send_message.call_args_list
    assert len(calls) == 4
    assert all("Ongoing" in call.args[1] or "LastDay" in call.args[1] for call in calls)
    assert len(svc.store.tournaments()) == 4
    assert svc.store.tournament_notice(3, 1) is None
    monkeypatch.setattr("betbot.service.utcnow", lambda: datetime(2026, 11, 1, 8, tzinfo=UTC))
    await svc.notify_tournaments()
    assert svc.bot.send_message.await_count == 6
    assert "November" in svc.bot.send_message.call_args.args[1]


async def test_old_distant_alert_can_return_in_its_month_without_overriding_skip(
    tournament_service,
):
    svc = tournament_service
    svc.store.remember_tournament({**EVENT, "start_date": "2026-11-01", "end_date": "2026-11-07"})
    for admin in (1, 2, 3):
        svc.store.begin_tournament_notice(1, admin)
        svc.store.finish_tournament_notice(1, admin, "sent", 91)
    svc.store.decide_tournament(1, 2, "skipped")
    svc.store.finish_tournament_notice(1, 3, "uncertain")
    svc.store.rearm_distant_tournament_notices("2026-10-31")
    assert svc.store.tournament_notice(1, 1)["state"] == "planned"
    assert svc.store.tournament_notice(1, 1)["message_id"] == 91
    assert svc.store.tournament_notice(1, 2)["state"] == "sent"
    assert svc.store.tournament_notice(1, 2)["decision"] == "skipped"
    assert svc.store.tournament_notice(1, 3)["state"] == "uncertain"


async def test_requested_repeat_includes_selected_tournament_and_preserves_decision(
    tournament_service, monkeypatch
):
    svc = tournament_service
    await svc.refresh_tournaments()
    await svc.notify_tournaments()
    svc.store.watch_tournament(1)
    svc.store.decide_tournament(1, 1, "watched")
    monkeypatch.setattr("betbot.service.asyncio.sleep", AsyncMock())
    await svc.notify_tournaments(repeat=True)
    assert svc.bot.send_message.await_count == 4
    assert svc.store.enabled(777)
    assert svc.store.tournament_notice(1, 1)["decision"] == "watched"
    await svc.notify_tournaments()
    assert svc.bot.send_message.await_count == 4
