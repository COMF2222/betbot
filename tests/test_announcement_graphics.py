import io
from datetime import datetime

import pytest
from PIL import Image

from betbot.announcement_graphics import render_schedule, render_tournament
from betbot.graphics import Canvas
from betbot.presentation import MOSCOW


@pytest.mark.parametrize("count", [0, 1, 4, 8, 15])
def test_tournament_card_text_stays_inside_canvas_with_long_names(monkeypatch, count):
    boxes = []
    original = Canvas.text

    def recording(self, *args, **kwargs):
        bounds = original(self, *args, **kwargs)
        boxes.append((bounds, self.width, self.height))
        return bounds

    monkeypatch.setattr(Canvas, "text", recording)
    event = {
        "name": "DreamLeague Season 30: South America Open Qualifier 1",
        "start_date": "2026-10-13",
        "end_date": "2026-10-15",
        "participants": [
            "The goal of all life is death",
            "Team Spirit Academy",
            "Yakult Brothers",
            "LGD Gaming",
        ]
        * (count // 4)
        + ["超长中文名字"] * (count % 4),
        "team_count": 24,
        "prize_usd": 750000,
        "online": True,
        "organizer": "ESL Gaming",
    }
    with Image.open(io.BytesIO(render_tournament(event))) as image:
        assert image.width == 1280
    assert all(
        0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
        for box, width, height in boxes
    )


def test_schedule_remains_in_bounds_with_live_tbd_and_long_team_names(monkeypatch):
    boxes = []
    original = Canvas.text

    def recording(self, *args, **kwargs):
        bounds = original(self, *args, **kwargs)
        boxes.append((bounds, self.width, self.height))
        return bounds

    monkeypatch.setattr(Canvas, "text", recording)
    now = datetime(2026, 10, 13, 10, tzinfo=MOSCOW)
    rows = [
        {
            "league": "DreamLeague Season 30: Eastern Europe Closed Qualifier",
            "teams": ["The goal of all life is death", "TBD"],
            "start": now,
            "live": i == 0,
            "best_of": 3,
            "market": i % 2 == 0,
            "source": "https://liquipedia.net/dota2/DreamLeague/30",
        }
        for i in range(17)
    ]
    with Image.open(io.BytesIO(render_schedule(rows, now))) as image:
        assert image.width == 1280
    assert all(
        0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
        for box, width, height in boxes
    )
