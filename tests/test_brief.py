from datetime import datetime

from betbot.brief import brief_caption, scheduled_series
from betbot.presentation import MOSCOW


def event(**changes):
    data = {
        "id": "one",
        "title": "Dota 2: A vs B - BLAST Slam Playoffs",
        "startTime": "2026-10-11T09:00:00Z",
        "teams": [{"name": "A"}, {"name": "B"}],
    }
    data.update(changes)
    return data


def test_morning_schedule_uses_only_selected_tournaments_and_local_day():
    now = datetime(2026, 10, 11, 10, tzinfo=MOSCOW)
    rules = [{"league_id": 19102, "name": "BLAST Slam", "keywords": ["BLAST Slam"]}]
    events = [
        event(),
        event(id="duplicate"),
        event(startTime="2026-10-10T09:00:00Z"),
        event(title="Dota 2: A vs B - DreamLeague"),
        event(closed=True),
        event(teams=[]),
        event(startTime="2026-10-11T06:00:00Z"),
    ]
    series = scheduled_series(events, rules, now)
    assert len(series) == 1 and series[0]["start"].hour == 12
    text = brief_caption(series, {"settled": 0, "late": {"settled": 7, "wins": 6}})
    assert "первая в 12:00 МСК" in text and "плей-офф" in text
    assert "разборы с задержкой: 6 из 7" in text
    assert "Вчера бот" not in text


def test_empty_or_ambiguous_schedule_does_not_invent_an_announcement():
    assert brief_caption([]) is None
    now = datetime(2026, 10, 11, 10, tzinfo=MOSCOW)
    rule = {"league_id": 1, "name": "BLAST", "keywords": ["BLAST Slam"]}
    assert not scheduled_series([event()], [rule, {**rule, "league_id": 2}], now)
