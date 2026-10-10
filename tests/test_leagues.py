from dataclasses import replace
from datetime import UTC, datetime, timedelta

from betbot.leagues import activity_snapshot, league_rows

NOW = datetime(2026, 10, 10, 17, 0, tzinfo=UTC)
CALENDAR = [
    {
        "league_id": 19102,
        "name": "BLAST SLAM VIII",
        "start_date": "2026-09-29",
        "end_date": "2026-10-11",
    }
]
CATALOG = [
    {"id": 65000, "name": "The International 2013", "enabled": 0},
    {"id": 19102, "name": "BLAST SLAM VIII", "enabled": 0},
    {"id": 20359, "name": "DreamLeague Season 30 Qualifiers", "enabled": 0},
]


def test_current_tournaments_use_dates_or_today_games_not_highest_id():
    activity = {"20359": {"last_match_at": NOW.isoformat()}}
    rows = league_rows(CATALOG, activity, CALENDAR, now=NOW)
    assert [r["id"] for r in rows] == [19102, 20359]
    assert all(r["id"] != 65000 for r in rows)
    assert len(league_rows(CATALOG, activity, CALENDAR, "archive", now=NOW)) == 3


def test_event_disappears_when_ended_without_fresh_games():
    assert not league_rows(CATALOG, {}, CALENDAR, now=NOW + timedelta(days=3))
    assert league_rows(CATALOG, {}, CALENDAR, query="blast", now=NOW)[0]["id"] == 19102


def test_stale_live_and_future_activity_do_not_make_tournament_current(draft):
    old = replace(draft, league_id=65000, source_updated_at=NOW - timedelta(hours=1))
    snapshot = activity_snapshot(
        [old], [{"leagueid": 65000, "start_time": int((NOW + timedelta(days=1)).timestamp())}], NOW
    )
    assert not snapshot
    assert not league_rows(CATALOG, snapshot, [], now=NOW)
