"""Shared visual language for forecast and daily report images."""

import io
import math
import re

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from .presentation import MOSCOW, font, international_font
from .timing import timing_note

WHITE, MUTED, GREEN, RED, GOLD = "#F7FAFF", "#B6C5D9", "#77EDB5", "#FF8DA0", "#FFD388"
CYAN, AMBER, PANEL = "#71F0E1", "#FFCA8A", "#152238"
BORDER, SURFACE, SUBTLE, INK = "#35465F", "#1C2C45", "#0E192A", "#102333"
CARD_MARGIN, CARD_GAP = 56, 24
HERO_START, HERO_STEP, HERO_WIDTH, HERO_HEIGHT = 488, 144, 136, 76


class Canvas:
    def __init__(self, height=720):
        self.width, self.height = 1280, height
        xx, yy = np.meshgrid(np.linspace(0, 1, 1280), np.linspace(0, 1, height))
        left = np.exp(-((xx + 0.16) ** 2 + (yy - 0.22) ** 2) / 0.22)
        right = np.exp(-((xx - 1.15) ** 2 + (yy - 0.84) ** 2) / 0.2)
        rgb = np.stack(
            (8 + 22 * right + 6 * left, 14 + 37 * left + 12 * right, 26 + 31 * left + 13 * right),
            axis=-1,
        ).astype(np.uint8)
        self.image = Image.fromarray(rgb)
        self.draw = ImageDraw.Draw(self.image)
        # Restrained edge accents leave the information area free of decoration.
        self.draw.line((1030, 0, 1280, 250), fill=BORDER, width=1)
        self.draw.line((1070, 0, 1280, 210), fill=SUBTLE, width=1)

    def text(
        self,
        x,
        y,
        value,
        size=22,
        color=WHITE,
        bold=False,
        width=None,
        right=False,
        fit=False,
        baseline=False,
        center=False,
        middle=False,
    ):
        value = str(value)
        face_for = international_font if re.search(r"[\u3000-\u9fff\uf900-\ufaff]", value) else font
        face = face_for(size, bold)
        if fit and width:
            while size > 12 and self.draw.textlength(value, font=face) > width:
                size -= 1
                face = face_for(size, bold)
        if width and self.draw.textlength(value, font=face) > width:
            while value and self.draw.textlength(value + "…", font=face) > width:
                value = value[:-1]
            value += "…"
        bounds = self.draw.textbbox((0, 0), value, font=face)
        if center or middle:
            mask, offset = face.getmask2(value, mode="L")
            ink = mask.getbbox()
            if ink:
                bounds = tuple(ink[i] + offset[i % 2] for i in range(4))
        # Bbox ends are exclusive; center the occupied pixels and snap to the raster.
        if center:
            x -= (bounds[0] + bounds[2] - 1) / 2
        elif right:
            x -= bounds[2]
        else:
            x -= bounds[0]
        if middle:
            y -= (bounds[1] + bounds[3] - 1) / 2
        elif baseline:
            y -= face.getmetrics()[0]
        x, y = round(x), round(y)
        self.draw.text((x, y), value, font=face, fill=color)
        return self.draw.textbbox((x, y), value, font=face)

    def header(self, subtitle, date, detail=""):
        d = self.draw
        d.rounded_rectangle((56, 35, 107, 86), radius=13, fill=CYAN)
        glyph = font(35, True).getmask("B", mode="L")
        mask = Image.frombytes("L", glyph.size, bytes(glyph))
        bounds = mask.point(lambda value: 255 if value == 255 else 0).getbbox() or mask.getbbox()
        x = round(81.5 - (bounds[0] + bounds[2] - 1) / 2)
        y = round(60.5 - (bounds[1] + bounds[3] - 1) / 2)
        self.image.paste(Image.new("RGB", mask.size, INK), (x, y), mask)
        self.text(125, 45, "BETBOT", 32, bold=True, middle=True)
        self.text(125, 80, subtitle, 18, MUTED, width=575, middle=True)
        self.text(1224, 45, date, 21, bold=True, right=True, width=480, middle=True)
        self.text(1224, 80, detail, 18, MUTED, right=True, width=510, middle=True)
        d.line((56, 119, 1224, 119), fill=BORDER, width=1)
        d.line((56, 119, 154, 119), fill=CYAN, width=3)

    def panel(self, box, accent=None, radius=18, fill=PANEL):
        self.draw.rounded_rectangle(box, radius=radius, fill=fill, outline=BORDER, width=1)
        if accent:
            x1, y1, _, y2 = box
            self.draw.rounded_rectangle((x1, y1 + 20, x1 + 4, y2 - 20), radius=2, fill=accent)

    def wrapped(self, x, y, value, size, width, color=WHITE, bold=True, line_height=36):
        """Two-line names with fixed columns, rather than shrinking an entire card."""
        face = font(size, bold)
        words = str(value).split()
        lines, line = [], ""
        for word in words:
            candidate = (line + " " + word).strip()
            if line and self.draw.textlength(candidate, font=face) > width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        if len(lines) > 2:
            lines = [lines[0], " ".join(lines[1:])]
        for index, line in enumerate(lines or ["—"]):
            self.text(
                x,
                y + (index - (len(lines) - 1) / 2) * line_height,
                line,
                size,
                color,
                bold,
                width=width,
                middle=True,
            )

    def stars(self, x, y, count, size=17):
        for i in range(5):
            points = []
            cx, cy = x + i * (size + 8) + size / 2, y + size / 2
            for k in range(10):
                angle = -math.pi / 2 + k * math.pi / 5
                radius = size / 2 if k % 2 == 0 else size * 0.23
                points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
            self.draw.polygon(
                points,
                fill=GOLD if i < count else SURFACE,
                outline=GOLD if i < count else MUTED,
            )

    def bar(self, y, probability, x=56, width=1168, colors=(CYAN, AMBER), height=12):
        d = self.draw
        edge = x + round(width * max(0, min(1, probability)))
        d.rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill=colors[1])
        if edge > x:
            d.rounded_rectangle((x, y, edge, y + height), radius=height // 2, fill=colors[0])
        if x + 4 < edge < x + width - 4:
            d.rectangle((edge - 2, y, edge + 2, y + height), fill=SUBTLE)

    def result(self, x, y, correct):
        color = MUTED if correct is None else GREEN if correct else RED
        self.draw.ellipse((x, y, x + 23, y + 23), fill=color)
        if correct is True:
            self.draw.line((x + 6, y + 12, x + 10, y + 16, x + 18, y + 7), fill="#101928", width=3)
        elif correct is False:
            self.draw.line((x + 7, y + 7, x + 16, y + 16), fill="#101928", width=3)
            self.draw.line((x + 16, y + 7, x + 7, y + 16), fill="#101928", width=3)
        else:
            self.draw.line((x + 7, y + 12, x + 16, y + 12), fill="#101928", width=3)

    def png(self):
        output = io.BytesIO()
        self.image.save(output, format="PNG", optimize=True)
        return output.getvalue()


def strength(probability):
    if probability < 0.55:
        return 0, "монетка"
    if probability < 0.6:
        return 1, "слабый сигнал"
    if probability < 0.65:
        return 2, "умеренный сигнал"
    if probability < 0.7:
        return 3, "заметный сигнал"
    if probability < 0.8:
        return 4, "сильный сигнал"
    return 5, "высокая вероятность"


def reliability_label(p, name, probability):
    _, label = strength(probability)
    bucket = p.reliability.get(name, {})
    n = bucket.get("count", 0)
    rate = bucket.get("success_rate")
    if n >= 100 and rate is not None:
        return f"{label} · тест: {round(rate * 100)} из 100 · n={n}"
    return f"{label} · частота пока не подтверждена"


def forecast_image(p, heroes=None, assets=None, demo=False):
    heroes = heroes or {}
    c = Canvas(840)
    d = c.draw
    radiant_percent = round(p.radiant_probability * 100)
    draft_percent = round(p.draft_probability * 100)
    map_label = f"карта {p.draft.map_number}" if p.draft.map_number else f"ID {p.draft.match_id}"
    c.header(
        "Прогноз на карту · Dota 2",
        p.draft.observed_at.astimezone(MOSCOW).strftime("%d.%m · %H:%M МСК"),
        (p.league_name + " · " if p.league_name else "") + map_label,
    )
    for side, y, name, players, color in (
        ("radiant", 146, p.draft.radiant_name, p.draft.radiant, CYAN),
        ("dire", 294, p.draft.dire_name, p.draft.dire, AMBER),
    ):
        c.panel((56, y, 1224, y + 132), color if p.favorite == side else None)
        d.rounded_rectangle((80, y + 12, 186, y + 36), radius=6, fill=SUBTLE)
        c.text(94, y + 24, "СВЕТ" if side == "radiant" else "ТЬМА", 14, color, True, middle=True)
        c.wrapped(80, y + 68, name, 32, 368)
        label = "Фаворит" if p.favorite == side and p.signal else "Основная оценка"
        percent = radiant_percent if side == "radiant" else 100 - radiant_percent
        c.text(80, y + 112, f"{percent}% · {label}", 15, color, True, width=368, middle=True)
        for index, player in enumerate(players[:5]):
            x = HERO_START + index * HERO_STEP
            path = assets / f"{player.hero_id}.png" if assets else None
            portrait = None
            if path and path.exists():
                try:
                    with Image.open(path) as image:
                        portrait = ImageOps.fit(
                            image.convert("RGB"),
                            (HERO_WIDTH, HERO_HEIGHT),
                            Image.Resampling.LANCZOS,
                        )
                except OSError:
                    pass
            if portrait:
                mask = Image.new("L", portrait.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, HERO_WIDTH - 1, HERO_HEIGHT - 1), radius=8, fill=255
                )
                c.image.paste(portrait, (x, y + 14), mask)
            else:
                d.rounded_rectangle(
                    (x, y + 14, x + HERO_WIDTH, y + 14 + HERO_HEIGHT), radius=8, fill=SURFACE
                )
                c.text(
                    x + HERO_WIDTH / 2,
                    y + 52,
                    f"#{player.hero_id}",
                    24,
                    color,
                    True,
                    width=HERO_WIDTH - 12,
                    center=True,
                    middle=True,
                )
            c.text(
                x + HERO_WIDTH / 2,
                y + 108,
                player.name or str(player.account_id),
                15,
                width=HERO_WIDTH,
                center=True,
                middle=True,
            )
            hero = heroes.get(player.hero_id, {}).get("localized_name", f"Hero #{player.hero_id}")
            c.text(
                x + HERO_WIDTH / 2,
                y + 125,
                hero,
                11,
                MUTED,
                width=HERO_WIDTH,
                center=True,
                middle=True,
            )

    c.panel((56, 444, 824, 675), CYAN if p.favorite == "radiant" else AMBER)
    c.text(80, 466, "ИГРОКИ + ДРАФТ", 16, bold=True, middle=True)
    d.rounded_rectangle((594, 452, 800, 480), radius=7, fill=SUBTLE)
    c.text(697, 466, "ОСНОВНОЙ ПРОГНОЗ", 12, CYAN, True, center=True, middle=True)
    color = CYAN if p.favorite == "radiant" else AMBER
    c.text(
        80, 535, f"{p.favorite_probability:.0%}", 114, color, True, width=285, fit=True, middle=True
    )
    c.wrapped(396, 535, p.favorite_name, 34, 404)
    level, _ = strength(p.favorite_probability)
    c.stars(80, 588, level, 18)
    c.text(
        238,
        597,
        reliability_label(p, "combined", p.favorite_probability),
        15,
        MUTED,
        width=562,
        middle=True,
    )
    c.bar(630, p.radiant_probability, 80, 720)
    c.text(80, 655, f"Свет {radiant_percent}%", 15, CYAN, True, middle=True)
    c.text(800, 655, f"Тьма {100 - radiant_percent}%", 15, AMBER, True, right=True, middle=True)

    c.panel((848, 444, 1224, 675))
    probability = p.draft_probability
    confidence = max(probability, 1 - probability)
    favorite = (
        p.draft.radiant_name
        if probability > 0.5
        else p.draft.dire_name
        if probability < 0.5
        else "Ровная карта"
    )
    color = CYAN if probability >= 0.5 else AMBER
    c.text(872, 466, "ТОЛЬКО ДРАФТ", 16, bold=True, middle=True)
    c.text(872, 522, f"{confidence:.0%}", 74, color, True, width=328, fit=True, middle=True)
    c.text(872, 566, favorite, 22, bold=True, width=328, middle=True)
    c.stars(872, 589, strength(confidence)[0], 15)
    bucket = p.reliability.get("draft", {})
    frequency = (
        f"Тест: {round(bucket['success_rate'] * 100)} из 100"
        if bucket.get("count", 0) >= 100 and bucket.get("success_rate") is not None
        else "Частота не подтверждена"
    )
    c.text(1000, 596.5, frequency, 13, MUTED, width=200, middle=True)
    c.bar(630, probability, 872, 328)
    c.text(872, 655, f"Свет {draft_percent}%", 15, CYAN, True, middle=True)
    c.text(1200, 655, f"Тьма {100 - draft_percent}%", 15, AMBER, True, right=True, middle=True)

    c.panel((56, 695, 1224, 785), fill=SURFACE)
    groups = {}
    for quote in p.quotes:
        if (
            quote.active
            and quote.market == "map_winner"
            and quote.outcome in ("radiant", "dire")
            and quote.match_id == p.draft.match_id
            and quote.map_number == p.draft.map_number
            and math.isfinite(quote.odds)
            and quote.odds > 1
        ):
            groups.setdefault(quote.source, {})[quote.outcome] = quote
    source = "Polymarket" if "Polymarket" in groups else next(iter(groups), None)
    if source:
        quotes = groups[source]
        quote_time = max(quote.observed_at for quote in quotes.values())
        label = "В момент прогноза"
        if (quote_time - p.draft.observed_at).total_seconds() > 30:
            label = f"На {quote_time.astimezone(MOSCOW):%H:%M} МСК · позже прогноза"
        if any(q.price_basis == "midpoint_fallback" for q in quotes.values()):
            label += " · ориентир по стакану"
        source_label = source.upper()
        if any(q.market_basis == "series_decider" for q in quotes.values()):
            source_label += " · РЕШАЮЩАЯ КАРТА"
        c.text(80, 718, source_label, 15, bold=True, width=420, middle=True)
        c.text(1200, 718, label, 14, MUTED, width=660, right=True, middle=True)
        d.line((640, 741, 640, 769), fill=BORDER, width=1)
        for outcome, team, x, price_x, color in (
            ("radiant", p.draft.radiant_name, 80, 608, CYAN),
            ("dire", p.draft.dire_name, 672, 1200, AMBER),
        ):
            quote = quotes.get(outcome)
            c.text(x, 756, team, 22, bold=True, width=366, middle=True)
            c.text(
                price_x,
                756,
                f"{quote.odds:.2f}" if quote else "—",
                32,
                color,
                True,
                width=140,
                fit=True,
                right=True,
                middle=True,
            )
    else:
        c.text(80, 718, "КОТИРОВКИ", 15, bold=True, middle=True)
        c.text(
            80, 756, "Подтверждённых цен на эту карту пока нет", 21, MUTED, width=1120, middle=True
        )
    footer = (
        "ДЕМОНСТРАЦИЯ · все команды и числа условные"
        if demo
        else "Звёзды — сила сигнала · частота — прошлые про-карты · цены до расходов"
    )
    if not demo and (note := timing_note(p)):
        footer = note + " Только пики и составы · отдельный учёт"
    c.text(56, 815, footer, 12, GOLD if demo else MUTED, width=1168, middle=True)
    return c.png()
