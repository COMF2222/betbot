"""Native Telegram admin screens: one message, clear parents, meaningful actions."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .domain import utcnow
from .leagues import league_rows
from .presentation import MOSCOW, esc, stats_text
from .providers import BOOKMAKERS


@dataclass
class Panel:
    text: str
    keyboard: InlineKeyboardMarkup


def button(text, data, style=None):
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def panel(text, rows, parent=None):
    if parent:
        rows.append(
            [button("⌂ Главная", "ui:home")]
            if parent == "ui:home"
            else [button("‹ Назад", parent), button("⌂ Главная", "ui:home")]
        )
    return Panel(text, InlineKeyboardMarkup(inline_keyboard=rows))


def home(service):
    count = len(service.store.leagues(True))
    active = sum(
        row["enabled"]
        for row in league_rows(
            service.store.leagues(), service.store.get("league_activity", {}), service.calendar
        )
    )
    mode = (
        "⏸ Публикации на паузе"
        if service.paused
        else "🟢 Автопубликация включена"
        if service.automatic
        else "👀 Предварительный просмотр"
    )
    if not service.model.ready:
        next_step = "Откройте раздел «Модель», чтобы подготовить прогнозы."
    elif not service.destinations:
        next_step = "Подключите группу или канал в разделе «Куда публиковать»."
    elif not active:
        next_step = "Выберите текущий турнир и включите отслеживание."
    elif not service.automatic:
        next_step = (
            "Предварительные прогнозы придут в личный чат. Автопубликация включается в настройках."
        )
    else:
        next_step = (
            "Бот следит за выбранными турнирами. Прогнозы появятся после полного свежего драфта."
        )
    destinations = (
        f"Включено {len(service.destinations)} мест" if service.destinations else "Не подключены"
    )
    text = f"🤖 <b>BETBOT · Админка</b>\n<i>Анализ Dota 2 и публикации в группах и каналах</i>\n\n<blockquote>{mode}\n🏆 Отслеживаем сейчас: <b>{active}</b> · всего выбрано {count}\n📣 Публикации: {destinations}</blockquote>\n\n{next_step}"
    return panel(
        text,
        [
            [button("🏆 Текущие турниры", "ui:leagues:current:0", "primary")],
            [
                button("🔔 Публикации", "ui:settings"),
                button("📣 Куда публиковать", "ui:destinations"),
            ],
            [button("💱 Источники", "ui:sources"), button("📊 Отчёты", "ui:stats:today")],
            [button("📬 Предпросмотры", "ui:pending"), button("🧠 Модель", "ui:model")],
            [button("🛠 Состояние", "ui:status"), button("❔ Помощь", "ui:help")],
        ],
    )


def tournaments(service, view="current", page=0, query=""):
    titles = {"current": "Сейчас", "selected": "Выбранные", "archive": "Весь каталог"}
    view = view if view in titles else "current"
    rows = league_rows(
        service.store.leagues(),
        service.store.get("league_activity", {}),
        service.calendar,
        view,
        query,
    )
    page = max(0, min(page, max(0, (len(rows) - 1) // 7)))
    selected = rows[page * 7 : (page + 1) * 7]
    text = f"🏆 <b>Турниры · {titles[view]}</b>\n"
    if query:
        text += f"Поиск: <b>{esc(query, 80)}</b>\n"
    if view == "current":
        text += "Идущие события по датам и турниры с матчами сегодня.\n"
    if selected:
        text += "\n" + "\n\n".join(
            f"{'🟢' if row['enabled'] else '⚪'} <b>{esc(row['name'], 85)}</b>\n{row['status']}"
            + (
                f" · {datetime.fromisoformat(row['calendar']['start_date']):%d.%m}–{datetime.fromisoformat(row['calendar']['end_date']):%d.%m}"
                if row["calendar_active"]
                else ""
            )
            for row in selected
        )
        text += f"\n\nСтраница {page + 1} из {max(1, (len(rows) + 6) // 7)} · {len(rows)} турниров"
    else:
        text += (
            "\nТекущих турниров по этому запросу не найдено."
            if view == "current"
            else "\nВыбранных турниров пока нет."
            if view == "selected"
            else "\nСовпадений не найдено."
        )
    buttons = [
        [button(titles[v], f"ui:leagues:{v}:0", "primary" if v == view else None) for v in titles]
    ]
    buttons += [
        [
            button(
                ("✓ " if r["enabled"] else "") + r["name"][:48],
                f"ui:league:{r['id']}:{view}:{page}",
            )
        ]
        for r in selected
    ]
    paging = []
    if page:
        paging.append(button("‹ Предыдущие", f"ui:leagues:{view}:{page - 1}"))
    if (page + 1) * 7 < len(rows):
        paging.append(button("Следующие ›", f"ui:leagues:{view}:{page + 1}"))
    if paging:
        buttons.append(paging)
    buttons.append(
        [
            button("🔎 Найти турнир", f"ui:search:{view}:{page}"),
            button("↻ Обновить", f"ui:league-refresh:{view}:{page}"),
        ]
    )
    if query:
        buttons.append([button("Сбросить поиск", f"ui:league-clear:{view}:0")])
    return panel(text, buttons, "ui:home")


def tournament(service, identity, view="current", page=0):
    rows = league_rows(
        service.store.leagues(),
        service.store.get("league_activity", {}),
        service.calendar,
        "archive",
    )
    row = next((r for r in rows if r["id"] == identity), None)
    parent = f"ui:leagues:{view}:{page}"
    if not row:
        return panel("Турнир не найден. Обновите список.", [], parent)
    event = row["calendar"]
    text = f"🏆 <b>{esc(row['name'], 120)}</b>\n\n{row['status']}\n{'🟢 Отслеживание включено' if row['enabled'] else '⚪ Отслеживание выключено'}"
    if event:
        start, end = event["start_date"].split("-"), event["end_date"].split("-")
        text += f"\n📅 {start[2]}.{start[1]}–{end[2]}.{end[1]}.{end[0]}"
    text += "\n\nПосле драфта бот подготовит прогноз. Автопубликация настраивается отдельно."
    live = service.store.get("live_coverage", {}).get("matches", [])
    for match in [m for m in live if m["league_id"] == identity][:5]:
        state = (
            "карта завершена"
            if match["status"] == "finished"
            else "разбор подготовлен"
            if match["forecast"]
            else match["reason"] or match["status"]
        )
        text += f"\n\n🧩 {esc(match['teams'], 95)}\n{esc(state, 130)} · {esc(match['source'], 30)}"
    if event.get("source"):
        text += f'\n<a href="{esc(event["source"], 250)}">Расписание организатора</a>'
    return panel(
        text,
        [
            [
                button(
                    "Отключить отслеживание" if row["enabled"] else "✓ Отслеживать турнир",
                    f"ui:watch:{identity}:{view}:{page}",
                    "danger" if row["enabled"] else "success",
                )
            ]
        ],
        parent,
    )


def publications(service, notice=""):
    mode = "Включена" if service.automatic else "Выключена"
    text = f"🔔 <b>Публикации</b>\n\nАвтопубликация: <b>{mode}</b>\nПауза: {'да' if service.paused else 'нет'}\nКарты: <b>{'все, включая ровные' if service.publish_all_maps else 'только выраженный фаворит'}</b>\nПорог выделения фаворита: <b>{service.min_probability:.0%}</b>\nРазборы после начала карты или без точного времени: {'включены, отдельный учёт' if service.allow_delayed_analysis else 'выключены'}\nТоталы времени: {'включены' if service.store.get('duration_enabled', False) else 'выключены'}\n\nБез автопубликации новые карточки доступны в личном чате для предварительного просмотра."
    if notice:
        text += f"\n\n<blockquote>{esc(notice, 250)}</blockquote>"
    return panel(
        text,
        [
            [
                button(
                    "Выключить автопубликацию"
                    if service.automatic
                    else "✓ Включить автопубликацию",
                    "ui:toggle:auto",
                    "danger" if service.automatic else "success",
                )
            ],
            [button("▶ Продолжить" if service.paused else "⏸ Пауза", "ui:toggle:pause")],
            [
                button(
                    "Все карты ✓" if service.publish_all_maps else "Только сигналы ✓",
                    "ui:toggle:all-maps",
                )
            ],
            [
                button(
                    "Дополнительные разборы ✓"
                    if service.allow_delayed_analysis
                    else "Дополнительные разборы: выкл.",
                    "ui:toggle:delayed",
                )
            ],
            [button("🎯 Порог сигнала", "ui:threshold"), button("⏱ Тоталы времени", "ui:duration")],
            [button("📅 Время отчётов", "ui:report-hours")],
            [
                button(
                    "Утренний анонс ✓"
                    if service.store.get(
                        "morning_brief_enabled", service.settings.morning_brief_enabled
                    )
                    else "Утренний анонс: выкл.",
                    "ui:toggle:morning",
                )
            ],
        ],
        "ui:home",
    )


def threshold(service):
    text = f"🎯 <b>Порог сигнала</b>\n\nСейчас: <b>{service.min_probability:.0%}</b>.\nВ режиме «все карты» бот публикует каждый полный драфт, а оценки ниже порога помечает как ровную карту. В режиме «только сигналы» публикуются карты от этого порога."
    values = (50, 55, 60, 65, 70, 75)
    buttons = [
        [
            button(
                f"{'✓ ' if abs(service.min_probability - v / 100) < 1e-8 else ''}{v}%",
                f"ui:set-threshold:{v}",
                "primary" if abs(service.min_probability - v / 100) < 1e-8 else None,
            )
            for v in values[i : i + 3]
        ]
        for i in (0, 3)
    ]
    return panel(text, buttons, "ui:settings")


def sources(service):
    active = service.store.get("source:Polymarket", True)
    text = (
        f"💱 <b>Источники котировок</b>\n\n<b>Polymarket</b> · {'включён' if active else 'выключен'}\nКэф — 1 / отображаемая цена рынка для конкретной карты. Цена покупки хранится отдельно; история рынка дополняет снимки во время игры.\n\n<b>1win · BetBoom · Winline · PARI</b>\n"
        + (
            "Поставщик настроен. Доступность конкретных площадок зависит от его линии."
            if service.feed or service.odds
            else "Для этих площадок пока нужен подтверждённый поставщик котировок."
        )
    )
    rows = [
        [
            button(
                "Выключить Polymarket" if active else "Включить Polymarket",
                "ui:source:Polymarket",
                "danger" if active else "success",
            )
        ],
        [button("🔗 Привязка рынков", "ui:market-help")],
    ]
    if service.feed or service.odds:
        rows += [
            [
                button(
                    ("✓ " if service.store.get(f"source:{book}", True) else "") + book,
                    f"ui:source:{book}",
                )
            ]
            for book in BOOKMAKERS
        ]
    return panel(text, rows, "ui:home")


def model(service):
    if not service.model.ready:
        text = "🧠 <b>Модель</b>\n\nМодель ещё не обучена. На сервере выполните <code>uv run betbot train</code>."
    else:
        report = service.model.raw["report"]
        text = f"🧠 <b>Рабочая модель</b>\n\n{'🟢 Порог публикации пройден' if service.model.publication_ready else '🟠 Пока только предварительный просмотр'}\n\nТест: <b>{report['test']} карт</b> · верно <b>{report['accuracy']:.1%}</b>\nBrier: {report['brier']:.4f} · Elo: {report['team_elo_brier']:.4f}\n\nЭто проверка всех доступных про-лиг. Отдельный тест Tier 1 доступен в отчётах."
    return panel(text, [[button("📊 Отчёты", "ui:stats:today")]], "ui:home")


def destinations(service, notice=""):
    targets = service.store.destinations()
    text = "📣 <b>Куда публиковать</b>\n\nПрогнозы и отчёты отправляются во все включённые группы и каналы одновременно. Один прогноз считается в статистике один раз.\n"
    rows = []
    for target in targets:
        kind = "Канал" if target["chat_type"] == "channel" else "Группа"
        text += f"\n{'🟢' if target['enabled'] else '⏸'} <b>{esc(target['title'], 70)}</b> · {kind.lower()}"
        rows.append(
            [
                button(
                    ("📣 " if target["chat_type"] == "channel" else "💬 ") + target["title"][:44],
                    f"ui:destination:{target['chat_id']}",
                )
            ]
        )
    if not targets:
        text += "\nПока ничего не подключено."
    if notice:
        text += f"\n\n<blockquote>{esc(notice, 300)}</blockquote>"
    rows.append(
        [
            button("＋ Группа", "ui:add-destination:group", "primary"),
            button("＋ Канал", "ui:add-destination:channel", "primary"),
        ]
    )
    return panel(text, rows, "ui:home")


def destination(service, chat_id, notice=""):
    target = next((t for t in service.store.destinations() if t["chat_id"] == chat_id), None)
    if not target:
        return panel("Место публикации не найдено.", [], "ui:destinations")
    kind = "Канал" if target["chat_type"] == "channel" else "Группа"
    text = f"📣 <b>{esc(target['title'], 100)}</b>\n\n{kind} · {'🟢 включено' if target['enabled'] else '⏸ выключено'}\nID: <code>{chat_id}</code>"
    if target["topic_id"]:
        text += f"\nТема: <code>{target['topic_id']}</code>"
    text += "\n\nРазбор и результат карты привязываются к исходному посту именно здесь."
    if notice:
        text += f"\n\n<blockquote>{esc(notice, 300)}</blockquote>"
    return panel(
        text,
        [
            [
                button(
                    "Отключить публикации" if target["enabled"] else "Включить публикации",
                    f"ui:destination-toggle:{chat_id}",
                    "danger" if target["enabled"] else "success",
                )
            ],
            [button("↻ Проверить доступ", f"ui:destination-check:{chat_id}")],
        ],
        "ui:destinations",
    )


def status(service):
    text = "🛠 <b>Состояние бота</b>\n\n"
    coverage = service.store.get("live_coverage", {})
    if coverage:
        matches = coverage.get("matches", [])
        text += f"Выбранные live-карты: <b>{len(matches)}</b> · с разбором: <b>{sum(m['forecast'] for m in matches)}</b>\n\n"
        for match in matches[:5]:
            state = (
                "карта завершена"
                if match["status"] == "finished"
                else "разбор подготовлен"
                if match["forecast"]
                else match["reason"] or match["status"]
            )
            text += f"{esc(match['teams'], 80)}\n{esc(state, 120)}\n\n"
    rows = service.store.health_rows()
    text += (
        "\n".join(
            f"{'🟢' if row['ok'] else '🟠'} <b>{esc(row['source'], 35)}</b>\n{esc(row['detail'], 130)}"
            for row in rows[:9]
        )
        or "Проверки источников ещё не завершены."
    )
    recent = service.store.recent_matches(3)
    if recent:
        text += "\n\n<b>Последние карты</b>\n" + "\n".join(
            f"{r['id']}: {esc(r['reason'] or r['status'], 110)}" for r in recent
        )
    for row in service.store.uncertain()[:3]:
        text += f"\n\n⚠️ Проверьте доставку: <code>/confirm {row['forecast_id']} {esc(row['kind'], 20)} MESSAGE_ID {row['destination']}</code>"
    return panel(text, [[button("↻ Обновить", "ui:status")]], "ui:home")


def statistics(service, period="today"):
    now = utcnow().astimezone(MOSCOW)
    today = datetime.combine(now.date(), time.min, MOSCOW)
    start, end, label = (
        (today, today + timedelta(days=1), "Сегодня")
        if period == "today"
        else (today - timedelta(days=1), today, "Вчера")
        if period == "yesterday"
        else (now - timedelta(days=7), now, "Последние 7 дней")
    )
    summary = service.store.summary(
        start.astimezone(utcnow().tzinfo).isoformat(), end.astimezone(utcnow().tzinfo).isoformat()
    )
    return panel(
        stats_text(summary, label),
        [
            [
                button("Сегодня", "ui:stats:today", "primary" if period == "today" else None),
                button("Вчера", "ui:stats:yesterday", "primary" if period == "yesterday" else None),
                button("7 дней", "ui:stats:week", "primary" if period == "week" else None),
            ],
            [button("🖼 Карточка отчёта", f"ui:report-card:{period}")],
            [button("⏱ Отчёт: отдельный учёт", f"ui:report-card:{period}:delayed")],
            [button("📄 Тест Tier 1", "ui:history"), button("💾 История CSV", "ui:export")],
        ],
        "ui:home",
    )
