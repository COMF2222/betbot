import io
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from betbot.graphics import CYAN, MUTED, PANEL, WHITE, forecast_image
from betbot.presentation import international_font
from betbot.reports import demo_report, render_report


@pytest.fixture(params=["default", "server"])
def text_boxes(monkeypatch, request):
    directories = [
        Path(__file__).resolve().parents[1] / "artifacts/fonts",
        Path("/usr/share/fonts/truetype/dejavu"),
    ]
    directory = next((p for p in directories if (p / "DejaVuSans.ttf").exists()), None)
    if directory and request.param == "server":

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


@pytest.mark.parametrize("layout", ["forecast", "report"])
def test_logo_glyph_is_centered_in_the_rendered_badge(prediction, text_boxes, layout):
    png = forecast_image(prediction) if layout == "forecast" else render_report(demo_report())
    with Image.open(io.BytesIO(png)) as image:
        badge = np.asarray(image.crop((56, 35, 108, 87)).convert("RGB"))
    rows, columns = np.nonzero(np.all(badge == (16, 35, 51), axis=2))
    assert len(rows) > 0
    assert abs(int(columns.min()) - (51 - int(columns.max()))) <= 1
    assert abs(int(rows.min()) - (51 - int(rows.max()))) <= 1


def test_hero_labels_share_the_center_of_their_portraits(prediction, text_boxes):
    forecast_image(prediction)
    for i, player in enumerate(prediction.draft.radiant):
        center = 488 + i * 144 + 68
        labels = [box for text, box in text_boxes if text == player.name]
        assert labels
        for box in labels:
            assert (box[0] + box[2]) / 2 == pytest.approx(center, abs=1)


def test_report_labels_align_with_result_icons(text_boxes):
    render_report(demo_report())
    rows = [box for _, box in text_boxes if 380 <= box[1] and box[3] <= 605]
    for box in rows:
        center = (box[1] + box[3]) / 2
        assert any(abs(center - (400 + i * 46)) <= 1 for i in range(5))


def test_japanese_nicks_render_real_glyphs_instead_of_identical_missing_boxes():
    face = international_font(18)
    assert bytes(face.getmask("鬼")) != bytes(face.getmask("誇"))


def test_design_text_colors_have_accessible_contrast():
    def luminance(color):
        values = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in values
        ]
        return sum(
            value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
        )

    for color in (WHITE, MUTED, CYAN):
        foreground, background = sorted((luminance(color), luminance(PANEL)))
        assert (background + 0.05) / (foreground + 0.05) >= 4.5
