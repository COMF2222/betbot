from dataclasses import replace
from pathlib import Path

import pytest
from PIL import ImageDraw, ImageFont

from betbot.graphics import forecast_image
from betbot.reports import demo_report, render_report


@pytest.fixture
def text_boxes(monkeypatch):
    directories = [
        Path(__file__).resolve().parents[1] / "artifacts/fonts",
        Path("/usr/share/fonts/truetype/dejavu"),
    ]
    directory = next((p for p in directories if (p / "DejaVuSans.ttf").exists()), None)
    if directory:

        def server_font(size, bold=False):
            return ImageFont.truetype(
                str(directory / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size
            )

        monkeypatch.setattr("betbot.graphics.font", server_font)
    original = ImageDraw.ImageDraw.text
    boxes = []

    def record(draw, xy, text, *args, **kwargs):
        face = kwargs.get("font")
        boxes.append((text, draw.textbbox(xy, text, font=face)))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    return boxes


def assert_legible(boxes, height):
    for text, (x1, y1, x2, y2) in boxes:
        assert 0 <= x1 < x2 <= 1280 and 0 <= y1 < y2 <= height, text
    for index, (text, a) in enumerate(boxes):
        for other, b in boxes[index + 1 :]:
            assert not (max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])), (
                text,
                other,
                a,
                b,
            )


@pytest.mark.parametrize(
    "probability,name",
    [
        (0.54, "NEW GROWTH"),
        (0.995, "Очень длинное название киберспортивной команды"),
        (0.7, "PARIVISION"),
        (0.5, "Team Yandex"),
    ],
)
def test_forecast_labels_never_overlap_with_server_font(prediction, text_boxes, probability, name):
    p = replace(
        prediction,
        radiant_probability=probability,
        draft_probability=probability,
        draft=replace(prediction.draft, radiant_name=name),
        late_analysis=True,
    )
    forecast_image(p)
    assert_legible(text_boxes, 840)


@pytest.mark.parametrize("wins,settled", [(6, 7), (0, 0), (9999, 12345)])
def test_daily_and_weekly_report_labels_fit_large_counts(text_boxes, wins, settled):
    data = demo_report()
    data.update(
        wins=wins, settled=settled, late_analysis=True, label="Итоги недели · разборы с задержкой"
    )
    for item in data["entries"]:
        item["label"] = "Очень длинная команда — Ещё одна длинная команда · карта 5"
        item["favorite"] = "Очень длинная команда"
        item["draft_favorite"] = "Ещё одна длинная команда"
    render_report(data)
    assert_legible(text_boxes, 720)
