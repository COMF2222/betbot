from betbot.config import Settings
from betbot.network import route_label, telegram_bot, telegram_proxy


def clear_proxies(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    monkeypatch.setattr("betbot.network.proxy_bypass", lambda host: False)


def test_explicit_telegram_proxy_wins_and_credentials_are_not_in_log_label(monkeypatch):
    clear_proxies(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    settings = Settings(
        _env_file=None,
        proxy_url="http://127.0.0.1:8888",
        telegram_proxy_url="socks5://user:secret@127.0.0.1:2080",
    )
    chosen = telegram_proxy(settings)
    assert chosen == settings.telegram_proxy_url
    assert route_label(chosen) == "socks5://127.0.0.1:2080"
    assert "secret" not in route_label(chosen)


def test_environment_proxy_and_no_proxy_are_respected(monkeypatch):
    clear_proxies(monkeypatch)
    settings = Settings(_env_file=None)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:2080")
    assert telegram_proxy(settings) == "http://127.0.0.1:2080"
    monkeypatch.setattr("betbot.network.proxy_bypass", lambda host: True)
    assert telegram_proxy(settings) is None


async def test_bot_uses_configured_proxy_and_short_request_timeout(monkeypatch):
    clear_proxies(monkeypatch)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        telegram_proxy_url="http://127.0.0.1:2080",
        telegram_request_timeout_seconds=20,
    )
    bot = telegram_bot(settings)
    try:
        assert bot.session.proxy == settings.telegram_proxy_url
        assert bot.session.timeout == 20
    finally:
        await bot.session.close()
