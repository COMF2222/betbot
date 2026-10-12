import asyncio
import csv
import io
import json
import logging
import re

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart, ExceptionTypeFilter
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    LinkPreviewOptions,
    MenuButtonCommands,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from pydantic import BaseModel, Field, ValidationError

from . import admin_ui as ui
from .config import Settings
from .demo import demo_prediction
from .destinations import BindingError, validate_destination
from .domain import utcnow
from .network import route_label, telegram_bot, telegram_proxy
from .presentation import caption, esc, explanation, render_card
from .providers import BOOKMAKERS, normalized
from .service import Service
from .storage import Store

log = logging.getLogger(__name__)

PUBLIC_COMMANDS = [("start", "Открыть бота"), ("help", "Помощь и команды")]
ADMIN_COMMANDS = [
    ("start", "Открыть админку"),
    ("admin", "Настройки и кнопки"),
    ("leagues", "Выбрать или найти турнир"),
    ("watch", "Включить турнир по ID"),
    ("unwatch", "Выключить турнир по ID"),
    ("status", "Проверить источники и отправки"),
    ("bind", "Подключить группу или канал"),
    ("model", "Проверка рабочей модели"),
    ("history", "Отчёт Tier 1 и CSV"),
    ("pending", "Предварительные прогнозы"),
    ("stats", "Итоги за 7 дней"),
    ("markets", "События Polymarket"),
    ("market", "Привязать рынок к карте"),
    ("map", "Подтвердить номер карты"),
    ("polyfilter", "Сопоставление турнира Polymarket"),
    ("alias", "Название команды на площадке"),
    ("threshold", "Порог публикации"),
    ("position", "Подтвердить позицию игрока"),
    ("refresh", "Обновить предварительный прогноз"),
    ("reports", "Часы дневного и недельного отчёта"),
    ("export", "Скачать историю прогнозов"),
    ("demo", "Посмотреть пример карточки"),
    ("help", "Все команды с параметрами"),
]


async def register_commands(bot: Bot, admins: set[int]) -> dict:
    public = [
        BotCommand(command=command, description=description)
        for command, description in PUBLIC_COMMANDS
    ]
    full = [
        BotCommand(command=command, description=description)
        for command, description in ADMIN_COMMANDS
    ]
    await bot.set_my_commands(public, scope=BotCommandScopeDefault())
    await bot.set_my_commands(public, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(
        [BotCommand(command="bind", description="Привязать эту группу к боту")],
        scope=BotCommandScopeAllGroupChats(),
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    failed = []
    for admin in sorted(admins):
        try:
            await bot.set_my_commands(full, scope=BotCommandScopeChat(chat_id=admin))
        except TelegramAPIError as exc:
            failed.append(admin)
            log.warning(
                "Не удалось установить личное меню для admin ID %s: %s", admin, type(exc).__name__
            )
    return {"admin_menus": len(admins) - len(failed), "unavailable_admin_ids": failed}


HELP = """🤖 <b>Betbot · админка</b>

/leagues [название] — найти турнир
/watch ID — включить турнир
/unwatch ID — выключить
/bind — места публикации; в группе подключает её
/bind @channel — подключить канал из личного чата
/admin — кнопки настроек
/pending — предварительные прогнозы
/status — источники и пропуски
/model — проверка модели
/history — исторический отчёт Tier 1 и CSV
/threshold 0.55 — порог выраженного сигнала; в режиме «все карты» ровные тоже публикуются
/stats — итоги за 7 дней
/export — история CSV
/demo — условный пример карточки

<b>Привязки рынков</b>
/map MATCH_ID НОМЕР — задать номер карты
/markets [команда] — каталог Polymarket
/market MATCH_ID EVENT_ID — подтвердить событие (и серию, если известна)
/polyfilter LEAGUE_ID ТЕКСТ — название турнира в заголовке Polymarket для автоматического сопоставления
/alias TEAM_ID ИМЯ — точное название команды на Polymarket
/refresh MATCH_ID — пересобрать только предварительный прогноз
/position MATCH_ID ACCOUNT_ID 1–5 — подтвердить позицию игрока на этой карте
/oddsbind MATCH_ID JSON — привязка рынков OddsPapi

/reports ЧАС_ДНЯ ЧАС_НЕДЕЛИ — часы МСК
/confirm ID forecast|result|explanation MESSAGE_ID [CHAT_ID] — сверить неопределённую отправку

Карточки сначала приходят сюда. Группы и каналы подключаются в «Куда публиковать». Для автопубликации нужна проверенная модель."""


class AdminGuard(BaseMiddleware):
    def __init__(self, admins: set[int]):
        self.admins = admins

    async def __call__(self, handler, event, data):
        user = event.from_user
        if not user or user.id not in self.admins:
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            elif event.chat.type == ChatType.PRIVATE:
                await event.answer(
                    f"Доступ только администратору. Ваш Telegram ID: <code>{user.id if user else 'не определён'}</code>. Владелец добавляет его в ADMIN_IDS."
                )
            return None
        message = event.message if isinstance(event, CallbackQuery) else event
        if not message:
            return None
        if message.chat.type != ChatType.PRIVATE:
            command = (getattr(message, "text", "") or "").split(" ")[0].split("@")[0]
            if not isinstance(event, Message) or command != "/bind":
                return None
        return await handler(event, data)


class OddsBinding(BaseModel):
    source: str
    fixture_id: str
    bookmaker_key: str
    market_id: str
    map_number: int = Field(ge=1, le=9)
    market: str = "map_winner"
    outcomes: dict[str, str]
    rules: str = Field(min_length=1, max_length=200)
    line: float | None = None


def menu(service: Service) -> InlineKeyboardMarkup:
    return ui.home(service).keyboard


def build_router(service: Service) -> Router:
    router = Router()
    guard = AdminGuard(service.settings.admins)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)

    async def show(target, screen):
        message = target.message if isinstance(target, CallbackQuery) else target
        if not isinstance(message, Message):
            return
        if isinstance(target, CallbackQuery) and message.text:
            try:
                await message.edit_text(
                    screen.text,
                    reply_markup=screen.keyboard,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        else:
            await message.answer(
                screen.text,
                reply_markup=screen.keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    def filters(user_id, view):
        return service.store.get(f"ui_filter:{user_id}:{view}", "")

    def clear_input(user_id):
        service.store.set(f"ui_input:{user_id}", None)

    async def leagues_reply(message: Message, query=""):
        if not service.store.leagues():
            await service.refresh_catalog()
        await service.refresh_league_activity()
        clear_input(message.from_user.id)
        service.store.set(f"ui_filter:{message.from_user.id}:current", query)
        await show(message, ui.tournaments(service, "current", query=query))

    async def status_reply(message: Message):
        await show(message, ui.status(service))

    @router.message(CommandStart())
    async def start(message: Message):
        clear_input(message.from_user.id)
        await show(message, ui.home(service))

    @router.message(Command("help"))
    async def help_message(message: Message):
        await show(message, ui.panel(HELP, [], "ui:home"))

    @router.message(Command("admin"))
    async def admin(message: Message):
        await start(message)

    @router.message(Command("leagues"))
    async def leagues(message: Message):
        args = message.text.split(maxsplit=1)
        await leagues_reply(message, args[1] if len(args) == 2 else "")

    @router.message(Command("watch", "unwatch"))
    async def watch(message: Message):
        args = message.text.split()
        if len(args) != 2 or not args[1].isdigit() or int(args[1]) <= 0:
            await message.answer(
                "Формат: /watch ID или /unwatch ID. ID можно найти через /leagues."
            )
            return
        league_id = int(args[1])
        row = next((r for r in service.store.leagues() if r["id"] == league_id), None)
        if not row:
            await service.refresh_catalog()
            row = next((r for r in service.store.leagues() if r["id"] == league_id), None)
        if not row:
            await message.answer("Турнир с таким ID не найден в OpenDota.")
            return
        enabled = args[0].split("@")[0] == "/watch"
        service.store.league(league_id, row["name"], enabled)
        service.store.audit("league", f"{message.from_user.id}:{league_id}:{enabled}")
        await message.answer(f"{'Включён' if enabled else 'Выключен'}: {esc(row['name'], 100)}.")

    @router.message(Command("bind"))
    async def bind(message: Message):
        clear_input(message.from_user.id)
        args = message.text.split(maxsplit=1)
        if message.chat.type == ChatType.PRIVATE and len(args) == 1:
            await show(message, ui.destinations(service))
            return
        reference = message.chat.id
        if message.chat.type == ChatType.PRIVATE:
            value = args[1].strip()
            if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}|-\d+", value):
                await show(
                    message,
                    ui.destinations(
                        service,
                        "Формат: /bind @channel или /bind -1001234567890. Для закрытого канала используйте кнопку «＋ Канал».",
                    ),
                )
                return
            reference = int(value) if value.startswith("-") else value
        try:
            chat = await validate_destination(message.bot, reference, message.from_user.id)
        except BindingError as exc:
            await message.answer(esc(str(exc), 350))
            return
        topic_id = message.message_thread_id if message.chat.id == chat.id else None
        service.store.destination(chat.id, chat.title or str(chat.id), chat.type, topic_id)
        service.store.audit("bind", f"{message.from_user.id}:{chat.id}:{chat.type}:{topic_id}")
        notice = f"Подключено: {chat.title or chat.id}. Остальные места публикации сохранены."
        if message.chat.type == ChatType.PRIVATE:
            await show(message, ui.destinations(service, notice))
        else:
            await message.answer(esc(notice, 250) + "\nНастройки доступны в личном чате: /admin.")

    @router.message(F.chat_shared)
    async def shared_chat(message: Message):
        state = service.store.get(f"ui_input:{message.from_user.id}")
        if (
            not state
            or state.get("kind") != "destination"
            or state["request_id"] != message.chat_shared.request_id
        ):
            await message.answer(
                "Выбор устарел. Откройте «Куда публиковать» и выберите группу или канал заново.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        try:
            chat = await validate_destination(
                message.bot, message.chat_shared.chat_id, message.from_user.id
            )
            if (chat.type == "channel") != (state["chat_type"] == "channel"):
                raise BindingError("Тип выбранного места не совпадает. Повторите выбор.")
            service.store.destination(chat.id, chat.title or str(chat.id), chat.type)
            service.store.audit("bind", f"{message.from_user.id}:{chat.id}:{chat.type}")
            notice = f"Подключено: {chat.title or chat.id}. Публикации включены."
        except BindingError as exc:
            notice = str(exc)
        clear_input(message.from_user.id)
        await message.answer(esc(notice, 350), reply_markup=ReplyKeyboardRemove())
        await message.bot.edit_message_text(
            ui.destinations(service).text,
            chat_id=message.chat.id,
            message_id=state["message_id"],
            reply_markup=ui.destinations(service).keyboard,
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message):
        clear_input(message.from_user.id)
        await message.answer("Выбор отменён. Меню: /start.", reply_markup=ReplyKeyboardRemove())

    @router.message(Command("status"))
    async def status(message: Message):
        await status_reply(message)

    @router.message(Command("stats"))
    async def stats(message: Message):
        await show(message, ui.statistics(service, "week"))

    @router.message(Command("model"))
    async def model(message: Message):
        service.model.reload()
        await show(message, ui.model(service))

    @router.message(Command("pending"))
    async def pending(message: Message):
        await show(message, pending_panel())

    @router.message(Command("history"))
    async def history_report(message: Message):
        root = service.settings.data_dir / "reports" / "tier1"
        path = root / "report.json"
        if not path.exists():
            await message.answer(
                "Сохранённого исторического отчёта пока нет. Выполните проверку Tier 1 на сервере; инструкция — README.md."
            )
            return
        report = json.loads(path.read_text(encoding="utf-8"))
        all_maps, signals = report["all_maps"], report["signals"]
        await message.answer(
            f"<b>Историческая проверка Tier 1</b>\n{esc(report['requested_start'][:10])} — {esc(report['requested_end'][:10])}\nВсе карты: {all_maps['wins']}/{all_maps['count']} · {all_maps['accuracy']:.1%}\nСигналы ≥ {report['signal_threshold']:.0%}: {signals['wins']}/{signals['count']} · {signals['accuracy']:.1%}\nПокрытие: {report['signal_coverage']:.1%}\nРетроспективный тест, без текущего исхода во входе модели. Доходность не измерена."
        )
        for name in ("report.md", "results.csv", "comparison.md"):
            artifact = root / name
            if artifact.exists() and artifact.stat().st_size < 8_000_000:
                await message.answer_document(
                    BufferedInputFile(artifact.read_bytes(), filename=name)
                )

    @router.message(Command("threshold"))
    async def threshold(message: Message):
        args = message.text.split()
        try:
            value = float(args[1]) if len(args) == 2 else 0
            if not 0.5 <= value <= 0.95:
                raise ValueError
        except ValueError:
            await message.answer(
                f"Текущий порог: {service.min_probability:.0%}. Формат: /threshold 0.55 (0.50–0.95). Фильтрует публикации в режиме «только сигналы»."
            )
            return
        service.store.set("min_favorite_probability", value)
        service.store.audit("threshold", f"{message.from_user.id}:{value}")
        await message.answer(
            f"Порог сигнала: {value:.0%}. В режиме «все карты» оценки ниже порога помечаются как ровные."
        )

    @router.message(Command("position"))
    async def position(message: Message):
        args = message.text.split()
        if len(args) != 4 or not all(x.isdigit() for x in args[1:]) or not 1 <= int(args[3]) <= 5:
            await message.answer(
                "Формат: /position MATCH_ID ACCOUNT_ID ПОЗИЦИЯ (1–5). Применяется только к этой карте."
            )
            return
        draft = service.store.draft(args[1])
        if not draft or int(args[2]) not in {p.account_id for p in draft.radiant + draft.dire}:
            await message.answer("Карта или игрок не найдены в наблюдаемом драфте.")
            return
        positions = service.store.get(f"positions:{args[1]}", {})
        positions[args[2]] = int(args[3])
        service.store.set(f"positions:{args[1]}", positions)
        service.store.audit("position", f"{message.from_user.id}:{':'.join(args[1:])}")
        await message.answer(
            "Позиция сохранена для этой карты. /refresh MATCH_ID пересоберёт свежий предварительный прогноз."
        )

    @router.message(Command("markets"))
    async def markets(message: Message):
        args = message.text.split(maxsplit=1)
        query = normalized(args[1]) if len(args) == 2 else ""
        try:
            events = await service.poly.discover()
            selected = [e for e in events if not query or query in normalized(e.get("title", ""))][
                :12
            ]
            lines = ["<b>Polymarket · события Dota 2</b>"]
            for event in selected:
                lines.append(
                    f"<code>{esc(str(event['id']), 25)}</code> · {esc(event.get('title', ''), 180)}"
                )
            lines.append(
                "Привязка: /market MATCH_ID EVENT_ID. Для автоматической привязки турнира: /polyfilter LEAGUE_ID НАЗВАНИЕ."
            )
            await message.answer("\n".join(lines))
        except Exception as exc:
            await message.answer("Ошибка каталога: " + esc(str(exc), 150))

    async def refresh_match(message: Message, match_id: str):
        draft = service.store.draft(match_id)
        if not draft:
            await message.answer("Карта ещё не наблюдалась. Проверьте ID и выбранный турнир.")
            return
        if service.store.has_forecast(match_id) and not service.store.supersede_preview(match_id):
            await message.answer("Опубликованный или неопределённый прогноз не заменяется.")
            return
        forecast_id = await service.process_draft(draft)
        if not forecast_id:
            row = service.store.match(match_id)
            await message.answer("Прогноз не создан: " + esc(row["reason"] or row["status"], 180))
        else:
            await message.answer(
                f"Предварительная версия сохранена: #{forecast_id}. Предыдущая остаётся в истории."
            )

    @router.message(Command("refresh"))
    async def refresh(message: Message):
        args = message.text.split()
        if len(args) != 2 or not args[1].isdigit():
            await message.answer("Формат: /refresh MATCH_ID")
            return
        await refresh_match(message, args[1])

    @router.message(Command("map"))
    async def map_number(message: Message):
        args = message.text.split()
        if (
            len(args) != 3
            or not args[1].isdigit()
            or not args[2].isdigit()
            or not 1 <= int(args[2]) <= 9
        ):
            await message.answer("Формат: /map MATCH_ID НОМЕР (1–9)")
            return
        service.store.set(f"map:{args[1]}", int(args[2]))
        service.store.audit("map", f"{message.from_user.id}:{args[1]}:{args[2]}")
        await message.answer(
            "Номер карты сохранён. /refresh MATCH_ID пересоберёт свежий предварительный прогноз."
        )

    @router.message(Command("market"))
    async def market(message: Message):
        args = message.text.split()
        if len(args) != 3 or not all(a.isdigit() for a in args[1:]):
            await message.answer("Формат: /market MATCH_ID EVENT_ID")
            return
        draft = service.store.draft(args[1])
        if not draft:
            await message.answer("Карта ещё не наблюдалась ботом.")
            return
        try:
            event = await service.poly.event(args[2])
            aliases = service.store.get("team_aliases", {})
            names = {normalized(t.get("name", "")) for t in event.get("teams", [])}
            expected = {
                normalized(aliases.get(str(draft.radiant_id), draft.radiant_name)),
                normalized(aliases.get(str(draft.dire_id), draft.dire_name)),
            }
            if names != expected:
                await message.answer(
                    "Команды события не совпадают. При различии названий сначала настройте /alias."
                )
                return
            service.store.set(f"poly:{draft.match_id}", args[2])
            if draft.series_id and draft.series_id != "0":
                service.store.set(f"poly_series:{draft.series_id}", args[2])
            service.store.audit("market", f"{message.from_user.id}:{draft.match_id}:{args[2]}")
            await message.answer(
                f"Событие подтверждено: {esc(event.get('title', ''), 180)}. Используйте /refresh MATCH_ID для свежего предварительного прогноза."
            )
        except Exception as exc:
            await message.answer("Источник недоступен: " + esc(str(exc), 140))

    @router.message(Command("polyfilter", "alias"))
    async def mapping(message: Message):
        args = message.text.split(maxsplit=2)
        if len(args) != 3 or not args[1].isdigit() or len(normalized(args[2])) < 3:
            await message.answer(
                "Формат: /polyfilter LEAGUE_ID НАЗВАНИЕ ТУРНИРА или /alias TEAM_ID ИМЯ КОМАНДЫ"
            )
            return
        if args[0].split("@")[0] == "/alias":
            aliases = service.store.get("team_aliases", {})
            aliases[args[1]] = args[2]
            service.store.set("team_aliases", aliases)
        else:
            service.store.set(f"poly_league:{args[1]}", args[2])
        service.store.audit("mapping", f"{message.from_user.id}:{args[0]}:{args[1]}:{args[2]}")
        await message.answer("Правило сохранено. Неоднозначные события всё равно не объединяются.")

    @router.message(Command("oddsbind"))
    async def oddsbind(message: Message):
        args = message.text.split(maxsplit=2)
        if len(args) != 3 or not args[1].isdigit():
            await message.answer(
                "Формат: /oddsbind MATCH_ID JSON. Схема и пример — docs/odds-feed.md."
            )
            return
        try:
            raw = json.loads(args[2])
            bindings = [
                OddsBinding.model_validate(item)
                for item in (raw if isinstance(raw, list) else [raw])
            ]
            for b in bindings:
                expected = {"radiant", "dire"} if b.market == "map_winner" else {"over", "under"}
                if (
                    b.source not in BOOKMAKERS
                    or b.market not in ("map_winner", "duration_total")
                    or set(b.outcomes) != expected
                ):
                    raise ValueError("Некорректная площадка или исходы")
                if b.market == "duration_total" and (b.line is None or b.line <= 0):
                    raise ValueError("Для тотала нужна положительная линия в минутах")
                if b.market == "map_winner" and b.line is not None:
                    raise ValueError("Для победителя карты линия не задаётся")
            service.store.set(f"odds:{args[1]}", [b.model_dump() for b in bindings])
            service.store.audit("oddsbind", f"{message.from_user.id}:{args[1]}")
            await message.answer(
                "Привязки сохранены. Они используются при настроенном ODDSPAPI_API_KEY и подтверждённом времени наблюдения котировок."
            )
        except (ValidationError, ValueError, TypeError):
            await message.answer(
                "Не удалось проверить JSON. Проверьте схему, исходы и линию; пример — docs/odds-feed.md."
            )

    @router.message(Command("reports"))
    async def reports(message: Message):
        args = message.text.split()
        if len(args) != 3 or not all(a.isdigit() and 0 <= int(a) <= 23 for a in args[1:]):
            await message.answer("Формат: /reports ЧАС_ДНЯ ЧАС_НЕДЕЛИ (0–23, МСК)")
            return
        service.store.set("daily_report_hour", int(args[1]))
        service.store.set("weekly_report_hour", int(args[2]))
        await message.answer(
            "Часы сохранены. Один итог за закрытый день после подтверждения результатов. Вечернее время означает отправку после полуночи."
        )

    @router.message(Command("confirm"))
    async def confirm(message: Message):
        args = message.text.split()
        if (
            len(args) not in (4, 5)
            or not args[1].isdigit()
            or not args[3].isdigit()
            or args[2] not in ("forecast", "result", "explanation")
            or len(args) == 5
            and not re.fullmatch(r"-\d+", args[4])
        ):
            await message.answer(
                "Формат: /confirm ID forecast|result|explanation MESSAGE_ID [CHAT_ID]. Используйте только после проверки поста в нужной группе или канале."
            )
            return
        ok = service.store.confirm_delivery(
            int(args[1]), args[2], int(args[3]), int(args[4]) if len(args) == 5 else None
        )
        await message.answer(
            "Отправка подтверждена в журнале."
            if ok
            else "Неопределённая отправка не найдена или требуется указать CHAT_ID из раздела «Состояние»."
        )

    @router.message(Command("export"))
    async def export(message: Message):
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(
            (
                "id",
                "match_id",
                "created_at",
                "state",
                "favorite",
                "probability",
                "model",
                "radiant_win",
                "duration_seconds",
            )
        )
        for row in service.store.forecasts(limit=100000):
            p = service.saved_prediction(row["payload"])
            result = json.loads(row["result"]) if row["result"] else {}
            name = p.favorite_name
            if name.startswith(("=", "+", "-", "@")):
                name = "'" + name
            writer.writerow(
                (
                    row["id"],
                    row["match_id"],
                    row["created_at"],
                    row["state"],
                    name,
                    p.favorite_probability,
                    p.model_version,
                    result.get("radiant_win", ""),
                    result.get("duration", ""),
                )
            )
        await message.answer_document(
            BufferedInputFile(out.getvalue().encode("utf-8-sig"), filename="forecasts.csv")
        )

    @router.message(Command("demo"))
    async def demo(message: Message):
        p = demo_prediction()
        await service.hero_assets(p.draft)
        await message.answer_photo(
            BufferedInputFile(
                render_card(p, service.heroes, service.assets, demo=True), filename="demo.png"
            ),
            caption="ДЕМОНСТРАЦИЯ: условные команды и числа. В статистику не входит.",
            reply_markup=ui.panel("", [[ui.button("🖼 Пример итогов дня", "ui:demo-day")]]).keyboard,
        )

    @router.callback_query(F.data.startswith("league:"))
    async def league_toggle(callback: CallbackQuery):
        league_id = int(callback.data.split(":")[1])
        row = next((r for r in service.store.leagues() if r["id"] == league_id), None)
        if not row:
            await callback.answer("Турнир не найден")
            return
        service.store.league(league_id, row["name"], not row["enabled"])
        service.store.audit("league", f"{callback.from_user.id}:{league_id}:{not row['enabled']}")
        await callback.answer("Выключен" if row["enabled"] else "Включён")
        await show(callback, ui.tournament(service, league_id))

    @router.callback_query(F.data.startswith("toggle:"))
    async def toggle(callback: CallbackQuery):
        kind = callback.data.split(":")[1]
        key = {"auto": "auto_publish", "pause": "paused", "duration": "duration_enabled"}.get(kind)
        if not key:
            await callback.answer("Неизвестная настройка")
            return
        current = service.automatic if kind == "auto" else service.store.get(key, False)
        if (
            kind == "auto"
            and not current
            and (not service.chat_id or not service.model.publication_ready)
        ):
            await callback.answer(
                "Сначала подключите место публикации и проверьте модель через /model.",
                show_alert=True,
            )
            return
        service.store.set(key, not current)
        service.store.audit("toggle", f"{callback.from_user.id}:{key}:{not current}")
        await callback.answer("Сохранено")
        await show(callback, ui.publications(service))

    @router.callback_query(F.data.startswith("source:"))
    async def source_toggle(callback: CallbackQuery):
        source = callback.data.split(":", 1)[1]
        if source not in ("Polymarket", *BOOKMAKERS):
            await callback.answer("Неизвестная площадка")
            return
        key = f"source:{source}"
        service.store.set(key, not service.store.get(key, True))
        await callback.answer("Настройка сохранена. Подключение требует рабочего источника данных.")

    @router.callback_query(F.data.startswith("menu:"))
    async def menu_action(callback: CallbackQuery):
        kind = callback.data.split(":")[1]
        await callback.answer()
        if kind == "leagues":
            await service.refresh_league_activity()
            await show(callback, ui.tournaments(service))
        elif kind == "status":
            await show(callback, ui.status(service))
        elif kind == "stats":
            await show(callback, ui.statistics(service))
        elif kind == "sources":
            await show(callback, ui.sources(service))

    def pending_panel():
        rows = service.store.forecasts("preview", 8)
        present = {r["id"] for r in rows}
        for identity in service.store.pending_publications():
            row = service.store.forecast(identity)
            p = service.saved_prediction(row["payload"])
            if (
                identity not in present
                and row["state"] != "superseded"
                and not row["result"]
                and 0
                <= (utcnow() - p.draft.observed_at).total_seconds()
                < service.settings.max_draft_age_seconds
            ):
                rows.append(row)
        rows = rows[:8]
        text = "📬 <b>Предварительные прогнозы</b>\n\n"
        buttons = []
        if not rows:
            text += "Новых карточек пока нет. Выберите турнир; бот ждёт полный свежий драфт. Причины пропусков видны в разделе «Состояние»."
            buttons = [
                [
                    ui.button("🏆 Текущие турниры", "ui:leagues:current:0"),
                    ui.button("🛠 Состояние", "ui:status"),
                ]
            ]
        for row in rows:
            p = service.saved_prediction(row["payload"])
            name = f"{p.favorite_name} {p.favorite_probability:.0%} · карта {p.draft.map_number or '?'}"
            buttons.append([ui.button(name[:55], f"ui:forecast:{row['id']}")])
        return ui.panel(text, buttons, "ui:home")

    @router.callback_query(F.data.startswith("ui:"))
    async def navigate(callback: CallbackQuery):
        args = callback.data.split(":")
        action, user_id = args[1], callback.from_user.id
        if action in (
            "tournaments",
            "tournament-refresh",
            "event",
            "event-watch",
            "event-skip",
            "event-unwatch",
        ) and (
            not isinstance(callback.message, Message)
            or callback.message.chat.type != ChatType.PRIVATE
            or callback.message.chat.id != user_id
        ):
            await callback.answer(
                "Управление новыми турнирами доступно в личной админке.", show_alert=True
            )
            return
        await callback.answer()
        clear_input(user_id)
        if action == "home":
            for view in ("current", "selected", "archive"):
                service.store.set(f"ui_filter:{user_id}:{view}", "")
            screen = ui.home(service)
        elif action in ("tournaments", "tournament-refresh"):
            page = max(0, int(args[2]))
            service.start_tournament_refresh(force=action == "tournament-refresh")
            screen = ui.tournament_catalog(service, user_id, page)
        elif action in ("event", "event-watch", "event-skip", "event-unwatch"):
            identity, page = int(args[2]), max(0, int(args[3]))
            notice = ""
            if service.store.tournament(identity) and action != "event":
                if action == "event-skip":
                    service.store.decide_tournament(identity, user_id, "skipped")
                    notice = "Повторных уведомлений не будет. Турнир сохранён в каталоге."
                else:
                    enabled = action == "event-watch"
                    event = service.store.watch_tournament(identity, enabled)
                    service.store.decide_tournament(
                        identity, user_id, "watched" if enabled else "skipped"
                    )
                    notice = (
                        "Турнир включён в отслеживание."
                        if enabled and event["league_id"]
                        else "Выбор сохранён."
                        if enabled
                        else "Отслеживание выключено."
                    )
                    service.update_tournament_calendar()
                service.store.audit("tournament", f"{user_id}:{identity}:{action}")
            screen = ui.tournament_alert(
                service, identity, user_id, notice, catalog=True, page=page
            )
        elif action in ("leagues", "league-refresh", "league-clear"):
            view, page = args[2], max(0, int(args[3]))
            if action == "league-clear":
                service.store.set(f"ui_filter:{user_id}:{view}", "")
            await service.refresh_league_activity(force=action == "league-refresh")
            screen = ui.tournaments(service, view, page, filters(user_id, view))
        elif action in ("league", "watch"):
            identity, view, page = int(args[2]), args[3], int(args[4])
            if action == "watch":
                row = next((r for r in service.store.leagues() if r["id"] == identity), None)
                if row:
                    service.store.league(identity, row["name"], not row["enabled"])
                    service.store.audit("league", f"{user_id}:{identity}:{not row['enabled']}")
            screen = ui.tournament(service, identity, view, page)
        elif action == "search":
            view, page = args[2], int(args[3])
            service.store.set(
                f"ui_input:{user_id}",
                {
                    "kind": "league-search",
                    "view": view,
                    "page": page,
                    "message_id": callback.message.message_id,
                    "chat_id": callback.message.chat.id,
                },
            )
            screen = ui.panel(
                "🔎 <b>Найти турнир</b>\n\nОтправьте название одним сообщением, например <b>BLAST</b> или <b>DreamLeague</b>.\nПоиск идёт в открытом разделе турниров.",
                [],
                f"ui:leagues:{view}:{page}",
            )
        elif action == "settings":
            screen = ui.publications(service)
        elif action == "toggle":
            kind = args[2]
            key = {
                "auto": "auto_publish",
                "pause": "paused",
                "duration": "duration_enabled",
                "all-maps": "publish_all_maps",
                "delayed": "allow_delayed_analysis",
                "morning": "morning_brief_enabled",
            }.get(kind)
            if not key:
                return
            current = {
                "auto": service.automatic,
                "all-maps": service.publish_all_maps,
                "delayed": service.allow_delayed_analysis,
                "morning": service.store.get(
                    "morning_brief_enabled", service.settings.morning_brief_enabled
                ),
            }.get(kind, service.store.get(key, False))
            notice = ""
            if (
                kind == "auto"
                and not current
                and (
                    not service.chat_id
                    or not service.model.publication_ready
                    or not service.store.leagues(True)
                )
            ):
                notice = "Для автопубликации нужны включённая группа или канал, проверенная модель и хотя бы один выбранный турнир."
            else:
                service.store.set(key, not current)
                service.store.audit("toggle", f"{user_id}:{key}:{not current}")
            screen = ui.publications(service, notice)
        elif action in ("threshold", "set-threshold"):
            if action == "set-threshold":
                value = int(args[2])
                if 50 <= value <= 95:
                    service.store.set("min_favorite_probability", value / 100)
                    service.store.audit("threshold", f"{user_id}:{value / 100}")
            screen = ui.threshold(service)
        elif action == "duration":
            text = "⏱ <b>Тоталы времени</b>\n\nЭкспериментальная оценка длительности. Вероятности ТБ/ТМ появляются, когда источник предоставляет подтверждённую линию. По умолчанию выключено."
            active = service.store.get("duration_enabled", False)
            screen = ui.panel(
                text,
                [
                    [
                        ui.button(
                            "Выключить" if active else "Включить",
                            "ui:toggle:duration",
                            "danger" if active else "success",
                        )
                    ]
                ],
                "ui:settings",
            )
        elif action in ("report-hours", "set-report-hour"):
            from .reports import daily_report_hour

            if action == "set-report-hour" and int(args[2]) in (0, 4):
                service.store.set("daily_report_hour", int(args[2]))
            hour = daily_report_hour(service.store.get("daily_report_hour", 0))
            screen = ui.panel(
                f"📅 <b>Время отчётов</b>\n\nДень: <b>{hour:02d}:00 МСК</b>.\nНеделя: понедельник, <b>{service.store.get('weekly_report_hour', 12):02d}:00 МСК</b>.\n\nОдин итог за закрытый день, когда подтверждены результаты всех опубликованных карт.",
                [
                    [
                        ui.button(
                            f"{'✓ ' if hour == h else ''}{h:02d}:00",
                            f"ui:set-report-hour:{h}",
                            "primary" if hour == h else None,
                        )
                        for h in (0, 4)
                    ]
                ],
                "ui:settings",
            )
        elif action in ("sources", "source"):
            if action == "source" and args[2] in ("Polymarket", *BOOKMAKERS):
                key = f"source:{args[2]}"
                service.store.set(key, not service.store.get(key, True))
            screen = ui.sources(service)
        elif action == "market-help":
            screen = ui.panel(
                "🔗 <b>Привязка рынков</b>\n\nНайдите событие через <code>/markets НАЗВАНИЕ КОМАНДЫ</code>.\nПодтвердите: <code>/market MATCH_ID EVENT_ID</code>.\nНомер карты: <code>/map MATCH_ID НОМЕР</code>.\n\nДля сопоставления турнира используйте <code>/polyfilter LEAGUE_ID НАЗВАНИЕ</code>. Случайные совпадения названий не привязываются.",
                [],
                "ui:sources",
            )
        elif action == "model":
            service.model.reload()
            screen = ui.model(service)
        elif action in ("group", "destinations"):
            screen = ui.destinations(service)
        elif action == "add-destination":
            kind = args[2]
            request_id = 9002 if kind == "channel" else 9001
            service.store.set(
                f"ui_input:{user_id}",
                {
                    "kind": "destination",
                    "chat_type": kind,
                    "request_id": request_id,
                    "message_id": callback.message.message_id,
                },
            )
            screen = ui.panel(
                "📣 <b>Подключение</b>\n\nСначала добавьте бота в выбранное место. В канале назначьте его администратором с правом «Публикация сообщений», в группе разрешите сообщения и фото.\n\nЗатем нажмите кнопку выбора под полем ввода. Для публичного канала также подходит <code>/bind @channel</code>.\nОтмена: <code>/cancel</code>.",
                [],
                "ui:destinations",
            )
            await callback.message.answer(
                "Выберите канал или группу через кнопку Telegram:",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [
                            KeyboardButton(
                                text="📣 Выбрать канал"
                                if kind == "channel"
                                else "💬 Выбрать группу",
                                request_chat=KeyboardButtonRequestChat(
                                    request_id=request_id,
                                    chat_is_channel=kind == "channel",
                                    bot_is_member=True,
                                    request_title=True,
                                ),
                            )
                        ],
                        [KeyboardButton(text="Отмена")],
                    ],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                ),
            )
        elif action in ("destination", "destination-toggle", "destination-check"):
            identity = int(args[2])
            target = next(
                (t for t in service.store.destinations() if t["chat_id"] == identity), None
            )
            notice = ""
            if target and action != "destination":
                try:
                    if action == "destination-check" or not target["enabled"]:
                        await validate_destination(callback.bot, identity, user_id)
                        notice = "Доступ подтверждён: бот может публиковать сообщения."
                    if action == "destination-toggle":
                        service.store.toggle_destination(identity)
                        service.store.audit(
                            "destination", f"{user_id}:{identity}:{not target['enabled']}"
                        )
                except BindingError as exc:
                    notice = str(exc)
            screen = ui.destination(service, identity, notice)
        elif action == "status":
            screen = ui.status(service)
        elif action == "stats":
            screen = ui.statistics(service, args[2])
        elif action == "history":
            await history_report(callback.message)
            return
        elif action == "export":
            await export(callback.message)
            return
        elif action == "help":
            screen = ui.panel(HELP, [[ui.button("🖼 Пример карточки", "ui:demo")]], "ui:home")
        elif action == "demo":
            await demo(callback.message)
            return
        elif action == "demo-day":
            from .reports import demo_report, render_report, report_caption

            data = demo_report()
            await callback.message.answer_photo(
                BufferedInputFile(render_report(data), filename="demo-day.png"),
                caption=report_caption(data),
            )
            return
        elif action == "pending":
            screen = pending_panel()
        elif action in ("forecast", "publish"):
            identity = int(args[2])
            notice = await service.publish(identity) if action == "publish" else ""
            row = service.store.forecast(identity)
            if not row:
                screen = ui.panel("Прогноз не найден.", [], "ui:pending")
            else:
                p = service.saved_prediction(row["payload"])
                text = (
                    "📬 <b>Предпросмотр</b>\n\n"
                    + caption(p, service.settings.quote_max_age_seconds, service.heroes)
                    + (f"\n\n{esc(notice, 300)}" if notice else "")
                )
                buttons = (
                    [
                        [
                            ui.button(
                                "Опубликовать во включённых местах",
                                f"ui:publish:{identity}",
                                "success",
                            )
                        ]
                    ]
                    if row["state"] == "preview" or identity in service.store.pending_publications()
                    else []
                )
                screen = ui.panel(text, buttons, "ui:pending")
        elif action == "report-card":
            from .reports import period_bounds, render_report, report_caption, report_payload

            start_at, end_at, label = period_bounds(args[2])
            late = len(args) > 3 and args[3] == "delayed"
            data = report_payload(service.store, start_at, end_at, label, late_analysis=late)
            if late:
                data["label"] += " · отдельный учёт"
            await callback.message.answer_photo(
                BufferedInputFile(render_report(data), filename="report.png"),
                caption=report_caption(data),
            )
            return
        else:
            screen = ui.home(service)
        await show(callback, screen)

    @router.callback_query(F.data.startswith("publish:"))
    async def publish(callback: CallbackQuery):
        await callback.answer()
        await callback.message.answer(await service.publish(int(callback.data.split(":")[1])))

    @router.callback_query(F.data.startswith("detail:"))
    async def detail(callback: CallbackQuery):
        await callback.answer()
        row = service.store.forecast(int(callback.data.split(":")[1]))
        if row:
            await callback.message.answer(
                explanation(service.saved_prediction(row["payload"]), service.heroes)
            )

    @router.message(F.chat.type == ChatType.PRIVATE, F.text)
    async def private_text(message: Message):
        if message.text.strip().casefold() == "отмена":
            await cancel(message)
            return
        state = service.store.get(f"ui_input:{message.from_user.id}")
        if state and state.get("kind") == "league-search":
            query = message.text.strip()[:80]
            clear_input(message.from_user.id)
            service.store.set(f"ui_filter:{message.from_user.id}:{state['view']}", query)
            screen = ui.tournaments(service, state["view"], query=query)
            await message.bot.edit_message_text(
                screen.text,
                chat_id=state["chat_id"],
                message_id=state["message_id"],
                reply_markup=screen.keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return
        text = message.text.strip().casefold()
        head, _, query = text.partition(" ")
        if head in ("start", "старт", "меню", "admin", "админка"):
            await start(message)
        elif head in ("help", "помощь"):
            await help_message(message)
        elif head in ("leagues", "лиги", "турниры"):
            await leagues_reply(message, query.strip())
        else:
            await message.answer(
                "Откройте меню команд или отправьте /help. Турниры: /leagues НАЗВАНИЕ.",
                reply_markup=menu(service),
            )

    @router.errors(ExceptionTypeFilter(TelegramNetworkError))
    async def network_error(event):
        service.store.health(
            "Telegram",
            False,
            "Таймаут отправки или получения. Проверьте VPN/прокси; диагностика: betbot doctor.",
        )
        log.error(
            "Telegram не ответил на запрос. Проверьте VPN/прокси; маршрут: %s. Отправка автоматически не повторяется.",
            route_label(telegram_proxy(service.settings)),
        )
        return True

    return router


async def run_bot(settings: Settings):
    if not settings.telegram_bot_token or not settings.admins:
        raise ValueError("Заполните TELEGRAM_BOT_TOKEN и ADMIN_IDS в .env. Пример: .env.example")
    settings.prepare()
    store = Store(settings.data_dir / "betbot.sqlite3")
    bot = telegram_bot(settings)
    service = Service(settings, store, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service))
    worker = None
    try:
        me = await bot.get_me()
        log.info(
            "Telegram подключён: @%s; маршрут: %s",
            me.username,
            route_label(telegram_proxy(settings)),
        )
        try:
            menus = await register_commands(bot, settings.admins)
            store.health(
                "Меню Telegram",
                not menus["unavailable_admin_ids"],
                f"Меню настроено для {menus['admin_menus']} администраторов",
            )
        except TelegramAPIError as exc:
            store.health("Меню Telegram", False, type(exc).__name__)
            log.warning(
                "Регистрация меню Telegram не завершилась: %s. /start остаётся доступен.",
                type(exc).__name__,
            )
        worker = asyncio.create_task(service.run())
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False
        )
    finally:
        service.stop.set()
        if worker:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        await service.close()
        await bot.session.close()
        store.close()


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
