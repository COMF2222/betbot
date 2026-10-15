from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    telegram_bot_token: str = ""
    admin_ids: str = ""
    telegram_chat_id: int | None = None
    telegram_topic_id: int | None = None
    proxy_url: str = ""
    telegram_proxy_url: str = ""
    telegram_request_timeout_seconds: int = 20
    data_dir: Path = Path("data")
    model_path: Path = Path("data/model.json")
    shadow_model_path: Path | None = None
    auto_publish: bool = False
    poll_seconds: int = 45
    max_draft_age_seconds: int = 180
    max_game_time_seconds: int = 90
    min_favorite_probability: float = 0.55
    publish_all_maps: bool = True
    allow_delayed_analysis: bool = True
    morning_brief_enabled: bool = True
    morning_brief_hour: int = 10
    quote_max_age_seconds: int = 30
    quote_wait_seconds: int = 120
    require_initial_odds: bool = True
    odds_alerts_enabled: bool = True
    odds_alert_relative_increase: float = 0.20
    odds_alert_min_increase: float = 0.20
    odds_alert_cooldown_seconds: int = 600
    odds_alert_max_per_map: int = 3
    opendota_api_key: str = ""
    steam_api_key: str = ""
    oddspapi_api_key: str = ""
    oddspapi_base_url: str = "https://api.oddspapi.io/v4"
    the_odds_api_key: str = ""
    oddspapi_daily_request_limit: int = 8
    oddspapi_prefetch_seconds: int = 86400
    odds_feed_url: str = ""
    odds_feed_api_key: str = ""

    @field_validator("morning_brief_hour")
    @classmethod
    def brief_hour(cls, value):
        if not 0 <= value <= 23:
            raise ValueError("Час анонса должен быть от 0 до 23")
        return value

    @field_validator(
        "poll_seconds",
        "max_draft_age_seconds",
        "quote_max_age_seconds",
        "telegram_request_timeout_seconds",
        "odds_alert_cooldown_seconds",
        "odds_alert_max_per_map",
        "oddspapi_daily_request_limit",
        "oddspapi_prefetch_seconds",
    )
    @classmethod
    def positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Значение должно быть положительным")
        return value

    @field_validator("odds_alert_relative_increase", "odds_alert_min_increase")
    @classmethod
    def price_increase(cls, value: float) -> float:
        if not 0 < value <= 10:
            raise ValueError("Порог изменения кэфа должен быть положительным и не больше 10")
        return value

    @field_validator("min_favorite_probability")
    @classmethod
    def threshold(cls, value: float) -> float:
        if not 0.5 <= value < 1:
            raise ValueError("Порог фаворита должен быть от 0.5 до 1")
        return value

    @field_validator("max_game_time_seconds")
    @classmethod
    def game_time(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Время игры должно быть неотрицательным")
        return value

    @field_validator("quote_wait_seconds")
    @classmethod
    def quote_wait(cls, value: int) -> int:
        if not 0 <= value <= 300:
            raise ValueError("Ожидание котировок должно быть от 0 до 300 секунд")
        return value

    @property
    def admins(self) -> set[int]:
        return {int(part.strip()) for part in self.admin_ids.split(",") if part.strip()}

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
