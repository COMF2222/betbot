import pytest

from betbot.config import Settings


def test_example_env_empty_optional_values_are_accepted(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_CHAT_ID=\nTELEGRAM_TOPIC_ID=\nTELEGRAM_BOT_TOKEN=\nADMIN_IDS=\n", encoding="utf-8"
    )
    settings = Settings(_env_file=path)
    assert settings.telegram_chat_id is None
    assert settings.telegram_topic_id is None
    assert settings.admins == set()


@pytest.mark.parametrize("seconds", [-1, 301])
def test_quote_wait_rejects_unbounded_or_negative_values(seconds):
    with pytest.raises(ValueError):
        Settings(_env_file=None, quote_wait_seconds=seconds)
