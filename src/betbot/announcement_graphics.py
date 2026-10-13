"""BETBOT schedule and tournament cards, using the shared forecast design tokens."""

from PIL import Image, ImageOps

from .announcements import date_range, logo_name, prize_label
from .graphics import AMBER, BORDER, CYAN, INK, MUTED, PANEL, SUBTLE, SURFACE, WHITE, Canvas
from .presentation import font

WEEKDAYS = "понедельник вторник среда четверг пятница суббота воскресенье".split()


def team_mark(canvas, name, x, y, size=56, assets=None):
    canvas.draw.rounded_rectangle(
        (x, y, x + size, y + size), radius=14, fill=SURFACE, outline=BORDER
    )
    if assets and (assets / logo_name(name)).is_file():
        try:
            with Image.open(assets / logo_name(name)) as source:
                logo = ImageOps.contain(source.convert("RGBA"), (size - 12, size - 12))
                canvas.image.paste(
                    logo, (x + (size - logo.width) // 2, y + (size - logo.height) // 2), logo
                )
            return
        except (OSError, ValueError):
            pass
    letters = "".join(word[0] for word in name.split()[:2]).upper() if name != "TBD" else "?"
    canvas.text(
        x + size / 2,
        y + size / 2,
        letters,
        round(size * 0.43),
        CYAN,
        bold=True,
        center=True,
        middle=True,
    )


def render_schedule(series, now, assets=None):
    shown = series[:7]
    canvas = Canvas(height=max(620, 344 + len(shown) * 112))
    canvas.header("Расписание · Dota 2", now.strftime("%d.%m.%Y"), WEEKDAYS[now.weekday()])
    canvas.text(56, 154, "Матчи дня", 46, bold=True)
    canvas.text(1224, 169, f"{len(series)} серий · время МСК", 25, MUTED, right=True, middle=True)
    for i, row in enumerate(shown):
        y = 220 + i * 112
        canvas.panel((56, y, 1224, y + 96), accent=CYAN if row.get("live") else None)
        canvas.draw.rounded_rectangle(
            (80, y + 20, 202, y + 76), radius=12, fill=CYAN if row.get("live") else SURFACE
        )
        canvas.text(
            141,
            y + 48,
            "LIVE" if row.get("live") else row["start"].strftime("%H:%M"),
            28,
            INK if row.get("live") else WHITE,
            bold=True,
            center=True,
            middle=True,
        )
        team_mark(canvas, row["teams"][0], 226, y + 18, assets=assets)
        canvas.text(298, y + 33, row["teams"][0], 34, bold=True, width=370, fit=True, middle=True)
        canvas.text(696, y + 33, "vs", 24, MUTED, center=True, middle=True)
        team_mark(canvas, row["teams"][1], 722, y + 18, assets=assets)
        canvas.text(794, y + 33, row["teams"][1], 34, bold=True, width=406, fit=True, middle=True)
        league = row["league"] + (f" · Bo{row['best_of']}" if row.get("best_of") else "")
        canvas.text(298, y + 72, league, 21, MUTED, width=766, middle=True)
        if row.get("market"):
            canvas.text(1200, y + 72, "РЫНОК", 16, AMBER, bold=True, right=True, middle=True)
    remaining = len(series) - len(shown)
    bottom = canvas.height - 90
    if remaining:
        canvas.text(56, bottom - 39, f"И ещё {remaining} серий сегодня", 24, MUTED)
    canvas.panel((56, bottom, 1224, bottom + 58), fill=SUBTLE)
    coverage = sum(bool(row.get("market")) for row in series)
    canvas.text(80, bottom + 28, "Прогнозы на карты — после драфта", 24, bold=True, middle=True)
    canvas.text(
        1200,
        bottom + 28,
        f"Polymarket: {coverage} из {len(series)}",
        21,
        MUTED,
        right=True,
        middle=True,
    )
    sources = (
        "Liquipedia · CC BY-SA"
        if any("liquipedia.net" in row.get("source", "") for row in series)
        else "Polymarket"
    )
    canvas.text(1224, canvas.height - 16, sources, 14, MUTED, right=True, middle=True)
    return canvas.png()


def render_tournament(event, assets=None):
    participants = event.get("participants", [])
    display = participants[:12]
    canvas = Canvas(height=952 if len(display) > 8 else 822 if len(display) > 4 else 692)
    canvas.header(
        "Анонс турнира · Dota 2",
        date_range(event),
        "онлайн" if event.get("online") else event.get("location", ""),
    )
    canvas.text(56, 153, "В ОТСЛЕЖИВАНИИ", 18, CYAN, bold=True)
    canvas.wrapped(56, 250, event["name"], 43, 1168, line_height=56)
    description = " · ".join(
        value for value in (event.get("organizer"), event.get("location")) if value
    )
    canvas.text(56, 308, description, 24, MUTED, width=1000)
    facts = [
        prize_label(event),
        f"{event['team_count']} команд" if event.get("team_count") else None,
        f"{len(participants)} подтверждено" if participants else None,
    ]
    x = 56
    for fact in (value for value in facts if value):
        width = round(canvas.draw.textlength(fact, font=font(25, True))) + 48
        canvas.draw.rounded_rectangle((x, 350, x + width, 404), radius=12, fill=SURFACE)
        canvas.text(
            x + width / 2,
            377,
            fact,
            25,
            AMBER if fact.startswith("$") else WHITE,
            bold=True,
            center=True,
            middle=True,
        )
        x += width + 16
    for index, name in enumerate(display):
        col, row = index % 4, index // 4
        x, y = 56 + col * 300, 438 + row * 130
        canvas.panel((x, y, x + 268, y + 112))
        team_mark(canvas, name, x + 20, y + 24, size=64, assets=assets)
        size = 25
        while size > 20 and any(
            canvas.draw.textlength(word, font=font(size, True)) > 150 for word in name.split()
        ):
            size -= 1
        canvas.wrapped(x + 100, y + 56, name, size, 150, color=WHITE, line_height=34)
    if not display:
        canvas.panel((56, 438, 1224, 550), fill=PANEL)
        canvas.text(640, 494, "Участники ещё объявляются", 30, MUTED, center=True, middle=True)
    extra = max(0, len(participants) - len(display))
    remaining = max(0, (event.get("team_count") or len(participants)) - len(participants))
    footer = (
        f"Ещё {extra} подтверждённых команд"
        if extra
        else f"Ещё {remaining} мест пока открыты"
        if remaining
        else "Состав участников объявлен"
        if participants
        else "Следим за обновлениями турнира"
    )
    y = canvas.height - 88
    canvas.draw.line((56, y - 18, 1224, y - 18), fill=BORDER)
    canvas.text(56, y, footer, 24, AMBER, bold=True)
    canvas.text(56, y + 38, "Разбираем каждую карту после драфта", 22, MUTED)
    canvas.text(
        1224, canvas.height - 16, "Liquipedia · CC BY-SA", 14, MUTED, right=True, middle=True
    )
    return canvas.png()
