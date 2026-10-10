"""Validate Telegram publication destinations without posting test messages."""

from aiogram.exceptions import TelegramAPIError


class BindingError(ValueError):
    pass


async def validate_destination(bot, reference, user_id=None):
    try:
        chat = await bot.get_chat(reference)
        if chat.type not in ("group", "supergroup", "channel"):
            raise BindingError("Можно подключить только группу или канал.")
        member = await bot.get_chat_member(chat.id, bot.id)
        if member.status in ("left", "kicked"):
            raise BindingError("Сначала добавьте бота в выбранную группу или канал.")
        if chat.type == "channel":
            if member.status != "administrator" or not member.can_post_messages:
                raise BindingError(
                    "Назначьте бота администратором канала с правом «Публикация сообщений» и повторите подключение."
                )
            if user_id:
                owner = await bot.get_chat_member(chat.id, user_id)
                if owner.status not in ("creator", "administrator"):
                    raise BindingError(
                        "Канал должен принадлежать вам или вы должны быть его администратором."
                    )
        elif member.status == "restricted":
            if not member.is_member or not member.can_send_messages or not member.can_send_photos:
                raise BindingError("Разрешите боту отправлять сообщения и фотографии в группе.")
        elif member.status == "member":
            permissions = getattr(chat, "permissions", None)
            if permissions and (
                permissions.can_send_messages is False or permissions.can_send_photos is False
            ):
                raise BindingError("Разрешите боту отправлять сообщения и фотографии в группе.")
        return chat
    except TelegramAPIError as exc:
        raise BindingError(
            "Telegram не подтвердил доступ. Проверьте адрес, добавьте бота и выдайте права публикации; затем повторите подключение."
        ) from exc
