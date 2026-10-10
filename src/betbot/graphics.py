"""Shared visual language for forecast and daily report images."""

import io
import math

import numpy as np
from PIL import Image, ImageDraw

from .presentation import MOSCOW, font

WHITE, MUTED, GREEN, RED, GOLD = "#f4f6fa", "#9dabc1", "#57dca3", "#ff7c94", "#e9bd7b"
CYAN, AMBER, PANEL = "#55dedc", "#e9bd7b", "#162136"


class Canvas:
    def __init__(self, height=720):
        self.width, self.height = 1280, height
        xx, yy = np.meshgrid(np.linspace(0, 1, 1280), np.linspace(0, 1, height))
        left = np.exp(-((xx + 0.1) ** 2 + (yy - 0.5) ** 2) / 0.17)
        right = np.exp(-((xx - 1.1) ** 2 + (yy - 0.42) ** 2) / 0.15)
        rgb = np.stack(
            (10 + 12 * right, 15 + 14 * left, 28 + 16 * left + 27 * right), axis=-1
        ).astype(np.uint8)
        self.image = Image.fromarray(rgb)
        self.draw = ImageDraw.Draw(self.image)
        self.draw.arc((980, -220, 1510, 310), 80, 265, fill="#29354b", width=1)
        self.draw.arc((940, -260, 1550, 350), 80, 265, fill="#233048", width=1)

    def text(self, x, y, value, size=22, color=WHITE, bold=False, width=None, right=False):
        face = font(size, bold)
        value = str(value)
        if width and self.draw.textlength(value, font=face) > width:
            while value and self.draw.textlength(value + "…", font=face) > width:
                value = value[:-1]
            value += "…"
        if right:
            x -= self.draw.textlength(value, font=face)
        self.draw.text((x, y), value, font=face, fill=color)

    def header(self, subtitle, date, detail=""):
        d = self.draw
        d.rounded_rectangle((56, 35, 107, 86), radius=13, fill=CYAN)
        self.text(70, 35, "B", 34, "#102333", True)
        self.text(125, 31, "BETBOT", 29, bold=True)
        self.text(125, 72, subtitle, 17, MUTED)
        self.text(1224, 37, date, 20, bold=True, right=True, width=480)
        self.text(1224, 73, detail, 17, MUTED, right=True, width=510)
        d.line((56, 119, 1224, 119), fill="#2b364b", width=1)

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
                fill=GOLD if i < count else "#29374b",
                outline=GOLD if i < count else "#607087",
            )

    def bar(self, y, probability, x=56, width=1168, colors=(CYAN, AMBER)):
        d = self.draw
        edge = x + round(width * max(0, min(1, probability)))
        d.rounded_rectangle((x, y, x + width, y + 10), radius=5, fill=colors[1])
        if edge > x:
            d.rounded_rectangle((x, y, edge, y + 10), radius=5, fill=colors[0])
        if x + 4 < edge < x + width - 4:
            d.rectangle((edge - 2, y, edge + 2, y + 10), fill="#0e1629")

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
    map_label = f"карта {p.draft.map_number}" if p.draft.map_number else f"ID {p.draft.match_id}"
    detail = (p.league_name + " · " if p.league_name else "") + map_label
    c.header(
        "прогноз после драфта · Dota 2",
        p.draft.observed_at.astimezone(MOSCOW).strftime("%d.%m · %H:%M МСК"),
        detail,
    )
    for side, y, name, players, color in (
        ("radiant", 147, p.draft.radiant_name, p.draft.radiant, CYAN),
        ("dire", 284, p.draft.dire_name, p.draft.dire, AMBER),
    ):
        d.rounded_rectangle(
            (56, y, 1224, y + 120), radius=16, fill=PANEL, outline="#2b3950", width=1
        )
        d.rounded_rectangle((56, y + 18, 61, y + 101), radius=2, fill=color)
        c.text(82, y + 17, "01 / СВЕТ" if side == "radiant" else "02 / ТЬМА", 13, color, True)
        c.text(82, y + 44, name, 29, bold=True, width=405)
        if p.favorite == side:
            c.text(82, y + 87, "ФАВОРИТ ОСНОВНОЙ МОДЕЛИ", 11, color, True)
        for i, player in enumerate(players[:5]):
            hx = 529 + i * 137
            path = assets / f"{player.hero_id}.png" if assets else None
            portrait = None
            if path and path.exists():
                try:
                    with Image.open(path) as img:
                        portrait = img.convert("RGB").resize((124, 70))
                except OSError:
                    pass
            if portrait:
                mask = Image.new("L", (124, 70), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, 123, 69), radius=7, fill=255)
                c.image.paste(portrait, (hx, y + 13), mask)
            else:
                d.rounded_rectangle((hx, y + 13, hx + 124, y + 83), radius=7, fill="#2b3a4d")
                c.text(hx + 30, y + 32, f"#{player.hero_id}", 23, color, True)
            c.text(hx, y + 86, player.name or str(player.account_id), 13, width=124)
            hero = heroes.get(player.hero_id, {}).get("localized_name", f"Hero #{player.hero_id}")
            c.text(hx, y + 103, hero, 10, MUTED, width=124)
    for title, probability, x, width, name in (
        ("ИГРОКИ + ДРАФТ / ОСНОВНАЯ ОЦЕНКА", p.radiant_probability, 56, 702, "combined"),
        ("ТОЛЬКО ДРАФТ / СРАВНЕНИЕ", p.draft_probability, 780, 444, "draft"),
    ):
        d.rounded_rectangle((x, 433, x + width, 675), radius=18, fill=PANEL)
        confidence = max(probability, 1 - probability)
        level, _ = strength(confidence)
        favorite = p.draft.radiant_name if probability >= 0.5 else p.draft.dire_name
        color = CYAN if probability >= 0.5 else AMBER
        c.text(x + 24, 449, title, 13, MUTED, True)
        if name == "combined":
            c.text(x + 22, 466, f"{confidence:.0%}", 86, color, True)
            c.text(x + 244, 490, favorite, 29, bold=True, width=width - 274)
            c.stars(x + 246, 539, level, 19)
            c.text(
                x + 244, 573, reliability_label(p, name, confidence), 13, MUTED, width=width - 268
            )
        else:
            c.text(x + 22, 480, f"{confidence:.0%}", 64, color, True)
            c.text(x + 164, 503, favorite, 23, bold=True, width=width - 186)
            c.stars(x + 24, 568, level, 15)
            c.text(x + 24, 595, reliability_label(p, name, confidence), 12, MUTED, width=width - 48)
        c.bar(625, probability, x + 24, width - 48)
        c.text(x + 24, 646, f"Свет {probability:.0%}", 13, CYAN)
        c.text(x + width - 24, 646, f"Тьма {1 - probability:.0%}", 13, AMBER, right=True)
    d.rounded_rectangle((56, 697, 1224, 780), radius=16, fill="#1b293f")
    groups = {}
    for q in p.quotes:
        if (
            q.active
            and q.market == "map_winner"
            and q.outcome in ("radiant", "dire")
            and q.match_id == p.draft.match_id
            and q.map_number == p.draft.map_number
        ):
            groups.setdefault(q.source, {})[q.outcome] = q
    source = "Polymarket" if "Polymarket" in groups else next(iter(groups), None)
    if source:
        quotes = groups[source]
        c.text(80, 710, source.upper() + " / ЦЕНЫ В МОМЕНТ ПРОГНОЗА", 12, MUTED, True, width=900)
        for outcome, team, x, color in (
            ("radiant", p.draft.radiant_name, 80, CYAN),
            ("dire", p.draft.dire_name, 677, AMBER),
        ):
            q = quotes.get(outcome)
            c.text(
                x, 738, f"{team}  {q.odds:.2f}" if q else f"{team}  —", 23, color, True, width=518
            )
    else:
        c.text(80, 710, "КОТИРОВКИ", 12, MUTED, True)
        c.text(80, 738, "Подтверждённых цен на эту карту пока нет", 22, MUTED)
    footer = (
        "ДЕМОНСТРАЦИЯ · все команды и числа условные"
        if demo
        else "Звёзды — сила сигнала · частота — прошлый временной тест, все про-лиги · цены до расходов"
    )
    c.text(56, 806, footer, 12, GOLD if demo else MUTED, width=1168)
    return c.png()
