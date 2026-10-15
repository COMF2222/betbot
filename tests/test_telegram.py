from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetChat,
    GetChatMember,
    GetMe,
    SendMessage,
    SendPhoto,
    SetChatMenuButton,
    SetMyCommands,
)
from aiogram.types import CallbackQuery, Chat, ChatShared, Message, Update, User

from betbot.config import Settings
from betbot.domain import utcnow
from betbot.presentation import MOSCOW
from betbot.service import Service
from betbot.storage import Store
from betbot.telegram import ADMIN_COMMANDS, AdminGuard, build_router, register_commands


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.fail_send = False
        self.can_post = True

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="Test", username="betbotdota2_bot")
        if isinstance(method, GetChat):
            is_channel = method.chat_id == -200 or isinstance(method.chat_id, str)
            return Chat(
                id=-200 if is_channel else method.chat_id,
                type="channel" if is_channel else "supergroup",
                title="Test channel" if is_channel else "Test group",
            )
        if isinstance(method, GetChatMember):
            return SimpleNamespace(status="administrator", can_post_messages=self.can_post)
        if isinstance(method, (SendMessage, EditMessageText)):
            if self.fail_send:
                raise TelegramNetworkError(method=method, message="Request timeout error")
            return Message(
                message_id=100,
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, (SetMyCommands, SetChatMenuButton, AnswerCallbackQuery)):
            return True
        if isinstance(method, SendPhoto):
            return Message(
                message_id=100, date=datetime.now(), chat=Chat(id=method.chat_id, type="private")
            )
        raise AssertionError(type(method).__name__)

    async def close(self):
        pass

    async def stream_content(self, url, **kwargs):
        yield b""


@pytest.fixture
async def routed_bot(tmp_path):
    settings = Settings(
        _env_file=None, admin_ids="1", data_dir=tmp_path, model_path=tmp_path / "missing.json"
    )
    store = Store(tmp_path / "state.sqlite3")
    store.league(123, "BLAST Slam Test")
    service = Service(settings, store)
    day = utcnow().astimezone(MOSCOW).date().isoformat()
    service.calendar = [
        {"league_id": 123, "name": "BLAST Slam Test", "start_date": day, "end_date": day}
    ]
    store.set("league_activity_checked_at", utcnow().isoformat())
    session = RecordingSession()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service))
    yield bot, dispatcher, service, session
    await service.close()
    await bot.session.close()
    store.close()


def update(text, chat_type="private", chat_id=1):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=1, is_bot=False, first_name="Admin"),
            text=text,
        ),
    )


@pytest.mark.parametrize("text", ["/start", "/start@betbotdota2_bot", "старт", "меню"])
async def test_actual_dispatcher_opens_admin_keyboard(routed_bot, text):
    bot, dispatcher, _, session = routed_bot
    await dispatcher.feed_update(bot, update(text))
    sent = [r for r in session.requests if isinstance(r, SendMessage)]
    assert len(sent) == 1
    assert sent[0].reply_markup.inline_keyboard
    assert "админка" in sent[0].text.casefold()


async def test_paper_bank_setup_and_stake_edit_through_real_dispatcher(routed_bot):
    from betbot.paper_bank import PaperBank

    bot, dispatcher, service, session = routed_bot
    await dispatcher.feed_update(bot, update("/bank"))
    assert "Виртуальный банк" in session.requests[-1].text
    await press(bot, dispatcher, "ui:paper-new")
    await dispatcher.feed_update(bot, update("10000 100"))
    bank = PaperBank(service.store)
    assert bank.summary(1)["initial_cents"] == 1000000
    assert bank.summary(1)["stake_cents"] == 10000
    assert service.store.get("ui_input:1") is None
    await press(bot, dispatcher, "ui:paper-stake")
    await dispatcher.feed_update(bot, update("250,50"))
    assert bank.summary(1)["stake_cents"] == 25050
    await press(bot, dispatcher, "ui:paper-pause")
    assert not bank.summary(1)["active"]
    await press(bot, dispatcher, "ui:paper-notify")
    assert not bank.summary(1)["notifications"]
    assert all(
        request.chat_id == 1
        for request in session.requests
        if isinstance(request, (SendMessage, EditMessageText))
    )


async def test_profit_research_panel_is_private_and_does_not_change_bank(routed_bot):
    from betbot.paper_bank import PaperBank

    bot, dispatcher, service, session = routed_bot
    bank = PaperBank(service.store)
    bank.start(1, 500000, 10000)
    before = bank.summary(1)
    await press(bot, dispatcher, "ui:profit-lab:early")
    assert "Исследование доходности" in session.requests[-1].text
    assert "Только подтверждённо ранние" in session.requests[-1].text
    assert "условными расходами 2%" in session.requests[-1].text
    await press(bot, dispatcher, "ui:profit-lab:all")
    assert "Все наблюдения" in session.requests[-1].text
    assert bank.summary(1) == before
    before_requests = len(session.requests)
    await press(bot, dispatcher, "ui:profit-lab:all", chat_id=-100, chat_type="group")
    assert len(session.requests) == before_requests


async def test_paper_bank_invalid_input_does_not_create_account_and_group_cannot_open_it(
    routed_bot,
):
    from betbot.paper_bank import PaperBank

    bot, dispatcher, service, session = routed_bot
    await press(bot, dispatcher, "ui:paper-new")
    await dispatcher.feed_update(bot, update("100 101"))
    assert PaperBank(service.store).summary(1) is None
    assert service.store.get("ui_input:1")["kind"] == "paper-new"
    before = len(session.requests)
    await dispatcher.feed_update(bot, update("/bank", "group", -100))
    assert len(session.requests) == before


async def test_paper_session_dates_use_moscow_even_across_utc_midnight(routed_bot):
    from datetime import UTC

    from betbot.paper_bank import PaperBank

    bot, dispatcher, service, session = routed_bot
    PaperBank(service.store).start(1, 100000, 10000, now=datetime(2026, 10, 13, 22, 30, tzinfo=UTC))
    await press(bot, dispatcher, "ui:paper-sessions")
    assert "14.10 01:30" in session.requests[-1].reply_markup.inline_keyboard[0][0].text


@pytest.mark.parametrize("text", ["/leagues", "лиги", "турниры BLAST"])
async def test_first_use_shows_available_leagues_without_selected_league(routed_bot, text):
    bot, dispatcher, _, session = routed_bot
    await dispatcher.feed_update(bot, update(text))
    sent = [r for r in session.requests if isinstance(r, SendMessage)]
    assert any("BLAST" in b.text for row in sent[0].reply_markup.inline_keyboard for b in row)


async def test_addressed_bind_sets_group_and_replies(routed_bot):
    bot, dispatcher, service, session = routed_bot
    await dispatcher.feed_update(bot, update("/bind@betbotdota2_bot", "supergroup", -100))
    assert service.chat_id == -100
    assert any(isinstance(r, SendMessage) and r.chat_id == -100 for r in session.requests)


async def test_private_channel_binding_keeps_existing_group_and_can_be_disabled(routed_bot):
    bot, dispatcher, service, _ = routed_bot
    await dispatcher.feed_update(bot, update("/bind", "supergroup", -100))
    await dispatcher.feed_update(bot, update("/bind @testchannel"))
    assert {t["chat_id"] for t in service.destinations} == {-100, -200}
    channel = next(t for t in service.destinations if t["chat_id"] == -200)
    assert channel["chat_type"] == "channel" and channel["topic_id"] is None
    await press(bot, dispatcher, "ui:destination-toggle:-200")
    assert {t["chat_id"] for t in service.destinations} == {-100}


async def test_channel_without_posting_permission_is_not_connected(routed_bot):
    bot, dispatcher, service, session = routed_bot
    session.can_post = False
    await dispatcher.feed_update(bot, update("/bind @testchannel"))
    assert not service.destinations
    assert any(
        isinstance(r, SendMessage) and "Публикация сообщений" in r.text for r in session.requests
    )


async def test_native_channel_selection_checks_request_and_binds_private_channel(routed_bot):
    bot, dispatcher, service, session = routed_bot
    await press(bot, dispatcher, "ui:add-destination:channel")
    picker = next(
        r
        for r in session.requests
        if isinstance(r, SendMessage) and getattr(r.reply_markup, "keyboard", None)
    )
    assert picker.reply_markup.keyboard[0][0].request_chat.chat_is_channel
    incoming = update("")
    incoming = incoming.model_copy(
        update={
            "message": incoming.message.model_copy(
                update={
                    "text": None,
                    "chat_shared": ChatShared(
                        request_id=9002, chat_id=-200, title="Private channel"
                    ),
                }
            )
        }
    )
    await dispatcher.feed_update(bot, incoming)
    assert service.destinations[0]["chat_type"] == "channel"
    assert service.store.get("ui_input:1") is None


async def test_unsolicited_shared_chat_cannot_bind_a_destination(routed_bot):
    bot, dispatcher, service, _ = routed_bot
    incoming = update("")
    incoming = incoming.model_copy(
        update={
            "message": incoming.message.model_copy(
                update={"text": None, "chat_shared": ChatShared(request_id=9002, chat_id=-200)}
            )
        }
    )
    await dispatcher.feed_update(bot, incoming)
    assert not service.destinations


async def test_network_error_is_logged_as_health_without_resending_message(routed_bot):
    bot, dispatcher, service, session = routed_bot
    session.fail_send = True
    await dispatcher.feed_update(bot, update("/start"))
    assert sum(isinstance(r, SendMessage) for r in session.requests) == 1
    assert any(r["source"] == "Telegram" and not r["ok"] for r in service.store.health_rows())


async def test_commands_registered_with_admin_private_scope_and_group_bind():
    session = RecordingSession()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", session=session)
    try:
        result = await register_commands(bot, {1})
        assert result["admin_menus"] == 1
        private = [
            r for r in session.requests if isinstance(r, SetMyCommands) and r.scope.type == "chat"
        ]
        assert private[0].scope.chat_id == 1
        assert {c.command for c in private[0].commands} == {name for name, _ in ADMIN_COMMANDS}
        groups = [
            r
            for r in session.requests
            if isinstance(r, SetMyCommands) and r.scope.type == "all_group_chats"
        ]
        assert [c.command for c in groups[0].commands] == ["bind"]
        assert any(isinstance(r, SetChatMenuButton) for r in session.requests)
    finally:
        await bot.session.close()


async def press(bot, dispatcher, data, chat_id=1, chat_type="private"):
    message = Message(
        message_id=100,
        date=datetime.now(),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
        text="Панель управления",
    )
    callback = CallbackQuery(
        id="test-callback",
        from_user=User(id=1, is_bot=False, first_name="Admin"),
        chat_instance="test",
        data=data,
        message=message,
    )
    await dispatcher.feed_update(bot, Update(update_id=2, callback_query=callback))


async def test_paper_band_amounts_and_custom_boundaries_through_dispatcher(routed_bot):
    from betbot.paper_bank import PaperBank, parse_bands

    bot, dispatcher, service, session = routed_bot
    bank = PaperBank(service.store)
    identity = bank.start(1, 1000000, 10000)
    await press(bot, dispatcher, f"ui:paper-rules:{identity}")
    panel = session.requests[-1]
    assert "1.30–1.49" in panel.text and "1.50–1.99" in panel.text
    assert panel.reply_markup.inline_keyboard[0][0].callback_data == f"ui:paper-band:{identity}:0"
    await press(bot, dispatcher, f"ui:paper-band:{identity}:1")
    await dispatcher.feed_update(bot, update("250,50"))
    assert [b["stake_cents"] for b in bank.rules(1)["bands"]] == [10000, 25050, 10000]
    assert service.store.get("ui_input:1") is None
    assert "Сохранено" in session.requests[-1].text
    await press(bot, dispatcher, f"ui:paper-band:{identity}:0")
    await dispatcher.feed_update(bot, update("0"))
    assert bank.rules(1)["bands"][0]["stake_cents"] == 0
    assert "1.30–1.49 → <b>пропуск</b>" in session.requests[-1].text
    await press(bot, dispatcher, f"ui:paper-bands:{identity}")
    await dispatcher.feed_update(bot, update("1.30 50\n1.70 120\n2.50 200"))
    assert bank.rules(1)["bands"] == parse_bands("1.30 50\n1.70 120\n2.50 200")
    await press(bot, dispatcher, "ui:paper-stake")
    await dispatcher.feed_update(bot, update("75"))
    assert bank.rules(1)["bands"] == parse_bands("1.30 75\n1.70 75\n2.50 75")
    assert all(
        request.chat_id == 1
        for request in session.requests
        if isinstance(request, (SendMessage, EditMessageText))
    )


async def test_paper_invalid_rules_keep_input_and_back_cancels_it(routed_bot):
    from betbot.paper_bank import PaperBank

    bot, dispatcher, service, session = routed_bot
    bank = PaperBank(service.store)
    identity = bank.start(1, 100000, 10000)
    before = bank.rules(1)
    await press(bot, dispatcher, f"ui:paper-bands:{identity}")
    await dispatcher.feed_update(bot, update("1.20 100"))
    assert bank.rules(1) == before
    assert service.store.get("ui_input:1")["kind"] == "paper-bands"
    assert "1.30" in session.requests[-1].text
    await press(bot, dispatcher, f"ui:paper-rules:{identity}")
    assert service.store.get("ui_input:1") is None
    assert session.requests[-1].reply_markup.inline_keyboard[-1][0].callback_data == (
        f"ui:paper-session:{identity}"
    )


@pytest.mark.parametrize("action", ["paper-band", "paper-bands", "paper-stake"])
async def test_pending_rule_input_cannot_change_replaced_bank(routed_bot, action):
    from betbot.paper_bank import PaperBank

    bot, dispatcher, service, session = routed_bot
    bank = PaperBank(service.store)
    old = bank.start(1, 100000, 10000)
    data = f"ui:paper-band:{old}:0" if action == "paper-band" else f"ui:{action}:{old}"
    await press(bot, dispatcher, data)
    new = bank.start(1, 500000, 50000)
    before = bank.rules(1)
    await dispatcher.feed_update(bot, update("1.30 50" if action == "paper-bands" else "50"))
    assert bank.rules(1) == before and bank.session(1)["id"] == new
    assert "уже закрыт" in session.requests[-1].text


async def test_pending_band_edit_rejects_changed_rule_version(routed_bot):
    from betbot.paper_bank import PaperBank, parse_bands

    bot, dispatcher, service, session = routed_bot
    bank = PaperBank(service.store)
    identity = bank.start(1, 100000, 10000)
    await press(bot, dispatcher, f"ui:paper-band:{identity}:0")
    bank.set_bands(1, parse_bands("1.50 150"))
    before = bank.rules(1)
    await dispatcher.feed_update(bot, update("99"))
    assert bank.rules(1) == before
    assert "Правила уже изменились" in session.requests[-1].text


async def test_paper_rules_cannot_expose_other_owner_or_be_edited_from_group(routed_bot):
    from betbot.paper_bank import PaperBank

    bot, dispatcher, service, session = routed_bot
    bank = PaperBank(service.store)
    own = bank.start(1, 100000, 10000)
    other = bank.start(2, 22222200, 222200)
    before = bank.rules(2)
    await press(bot, dispatcher, f"ui:paper-band:{other}:0")
    assert service.store.get("ui_input:1") is None
    assert "222" not in session.requests[-1].text
    requests_before = len(session.requests)
    await press(bot, dispatcher, f"ui:paper-band:{own}:0", chat_id=-100, chat_type="group")
    assert service.store.get("ui_input:1") is None
    assert len(session.requests) == requests_before
    assert bank.rules(2) == before


async def test_price_alert_panel_can_toggle_and_set_threshold(routed_bot):
    bot, dispatcher, service, session = routed_bot
    await press(bot, dispatcher, "ui:odds-alerts")
    assert "Движение кэфов" in session.requests[-1].text
    assert service.odds_alerts_enabled
    await press(bot, dispatcher, "ui:odds-alerts:toggle")
    assert not service.odds_alerts_enabled
    await press(bot, dispatcher, "ui:odds-alerts:threshold:10")
    assert service.odds_alert_relative_increase == 0.10
    assert "10%" in session.requests[-1].text


async def test_new_tournament_can_be_skipped_and_enabled_later_from_catalog(routed_bot):
    bot, dispatcher, service, session = routed_bot
    day = utcnow().astimezone(MOSCOW).date().isoformat()
    service.store.remember_tournament(
        {
            "source_key": "liquipedia:Test/Cup",
            "name": "New Cup",
            "tier": 2,
            "start_date": day,
            "end_date": day,
            "league_id": 456,
            "source": "https://liquipedia.net/dota2/Test/Cup",
        }
    )
    service.store.set("tournament_catalog_checked_at", utcnow().isoformat())
    await press(bot, dispatcher, "ui:event-skip:1:0")
    assert service.store.tournament_notice(1, 1)["decision"] == "skipped"
    assert not service.store.enabled(456)
    await press(bot, dispatcher, "ui:tournaments:0")
    assert "New Cup" in session.requests[-1].text
    await press(bot, dispatcher, "ui:event-watch:1:0")
    await press(bot, dispatcher, "ui:event-watch:1:0")
    assert service.store.enabled(456)
    assert service.store.tournament_notice(1, 1)["decision"] == "watched"
    assert "Отслеживание включено" in session.requests[-1].text


async def test_private_admin_can_request_current_month_alerts_again(routed_bot):
    bot, dispatcher, service, session = routed_bot
    service.store.set("tournament_catalog_checked_at", utcnow().isoformat())
    await press(bot, dispatcher, "ui:tournament-repeat:0")
    assert service.store.get("tournament_alert_repeat_requested") is True
    assert "до конца месяца" in session.requests[-1].text


@pytest.mark.parametrize("user_id,chat_id,chat_type", [(1, -100, "supergroup"), (7, 7, "private")])
async def test_tournament_action_requires_private_admin_chat(
    routed_bot, user_id, chat_id, chat_type
):
    bot, dispatcher, service, _ = routed_bot
    day = utcnow().astimezone(MOSCOW).date().isoformat()
    service.store.remember_tournament(
        {
            "source_key": "liquipedia:Test/Cup",
            "name": "New Cup",
            "tier": 2,
            "start_date": day,
            "end_date": day,
            "league_id": 456,
            "source": "https://liquipedia.net/dota2/Test/Cup",
        }
    )
    message = Message(
        message_id=100,
        date=datetime.now(),
        chat=Chat(id=chat_id, type=chat_type),
        text="Уведомление",
    )
    callback = CallbackQuery(
        id="alert",
        from_user=User(id=user_id, is_bot=False, first_name="User"),
        chat_instance="test",
        data="ui:event-watch:1:0",
        message=message,
    )
    await dispatcher.feed_update(bot, Update(update_id=2, callback_query=callback))
    assert not service.store.enabled(456)


async def test_navigation_edits_one_message_and_returns_to_correct_parent(routed_bot):
    bot, dispatcher, service, session = routed_bot
    for target in (
        "ui:leagues:current:0",
        "ui:league:123:current:0",
        "ui:watch:123:current:0",
        "ui:leagues:current:0",
        "ui:home",
    ):
        await press(bot, dispatcher, target)
    assert service.store.enabled(123)
    assert not any(isinstance(r, SendMessage) for r in session.requests)
    edits = [r for r in session.requests if isinstance(r, EditMessageText)]
    assert len(edits) == 5 and all(r.message_id == 100 for r in edits)
    back = edits[1].reply_markup.inline_keyboard[-1][0]
    assert back.callback_data == "ui:leagues:current:0"


async def test_search_returns_results_to_same_panel_and_cancel_clears_input(routed_bot):
    bot, dispatcher, service, session = routed_bot
    await press(bot, dispatcher, "ui:search:current:0")
    await dispatcher.feed_update(bot, update("BLAST"))
    assert not any(isinstance(r, SendMessage) for r in session.requests)
    edits = [r for r in session.requests if isinstance(r, EditMessageText)]
    assert "BLAST" in edits[-1].text
    await press(bot, dispatcher, "ui:search:current:0")
    await press(bot, dispatcher, "ui:home")
    assert service.store.get("ui_input:1") is None


async def test_demo_forecast_and_day_images_never_enter_real_ledger(routed_bot):
    bot, dispatcher, service, session = routed_bot
    service.hero_assets = AsyncMock()
    await dispatcher.feed_update(bot, update("/demo"))
    await press(bot, dispatcher, "ui:demo-day")
    photos = [r for r in session.requests if isinstance(r, SendPhoto)]
    assert len(photos) == 2
    assert all("ДЕМОНСТРАЦИЯ" in r.caption for r in photos)
    assert not service.store.forecasts()
    assert service.store.summary()["published"] == 0


async def test_nonadmin_cannot_execute_commands(monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    message = Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=2, type="private"),
        from_user=User(id=2, is_bot=False, first_name="Test"),
        text="/watch 123",
    )
    handler = AsyncMock()
    await AdminGuard({1})(handler, message, {})
    handler.assert_not_awaited()
    answer.assert_awaited_once()


async def test_group_commands_are_limited_to_authorized_bind():
    handler = AsyncMock()
    base = dict(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=-100, type="supergroup"),
        from_user=User(id=1, is_bot=False, first_name="Admin"),
    )
    guard = AdminGuard({1})
    await guard(handler, Message(**base, text="/watch 123"), {})
    handler.assert_not_awaited()
    await guard(handler, Message(**base, text="/bind@betbot"), {})
    handler.assert_awaited_once()
