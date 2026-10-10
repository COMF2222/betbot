"""Explicit proxy routing shared by runtime and diagnostics; no credentials in logs."""

import os
from urllib.parse import urlsplit
from urllib.request import proxy_bypass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from .config import Settings


def telegram_proxy(settings: Settings) -> str | None:
    explicit = settings.telegram_proxy_url or settings.proxy_url
    if explicit:
        return explicit
    if proxy_bypass("api.telegram.org"):
        return None
    for key in ("HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY"):
        value = os.environ.get(key) or os.environ.get(key.lower())
        if value:
            return value
    return None


def route_label(proxy: str | None) -> str:
    if not proxy:
        return "прямое соединение"
    try:
        parsed = urlsplit(proxy)
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 'default'}"
    except ValueError:
        return "настроенный прокси"


def telegram_bot(settings: Settings) -> Bot:
    session = AiohttpSession(
        proxy=telegram_proxy(settings), timeout=settings.telegram_request_timeout_seconds
    )
    return Bot(
        settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
