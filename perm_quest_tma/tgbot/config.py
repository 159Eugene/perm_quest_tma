import json
import os
from typing import List, Any, Optional
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DbConfig(BaseSettings):
    """
    Конфигурация подключения к базе данных PostgreSQL.
    Валидирует параметры и автоматически собирает URL для асинхронного драйвера asyncpg.
    """
    host: str = Field(default="localhost", validation_alias="DB_HOST")
    port: int = Field(default=5432, validation_alias="DB_PORT")
    user: str = Field(default="postgres", validation_alias="DB_USER")
    password: str = Field(default="postgres", validation_alias="DB_PASSWORD")
    name: str = Field(default="quest_db", validation_alias="DB_NAME")

    @property
    def database_url(self) -> str:
        """
        Собирает асинхронную строку подключения SQLAlchemy с драйвером asyncpg.
        """
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


class RedisConfig(BaseSettings):
    """
    Конфигурация подключения к Redis для хранения состояний FSM и очередей APScheduler.
    """
    host: str = Field(default="localhost", validation_alias="REDIS_HOST")
    port: int = Field(default=6379, validation_alias="REDIS_PORT")

    @property
    def redis_url(self) -> str:
        """
        Собирает строку подключения к Redis.
        """
        return f"redis://{self.host}:{self.port}/0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


class BotConfig(BaseSettings):
    """
    Конфигурация самого Telegram-бота, систем мониторинга и API погоды.
    """
    token: SecretStr = Field(..., validation_alias="BOT_TOKEN")
    admin_ids: List[int] = Field(default=[], validation_alias="ADMIN_IDS")
    
    # Скрытый канал для хранения файлов (фото, аудио), загружаемых через админ-панель
    dump_channel_id: Optional[int] = Field(default=None, validation_alias="DUMP_CHANNEL_ID")

    # Системные параметры мониторинга и метрик
    admin_alerts_chat_id: int = Field(default=0, validation_alias="ADMIN_ALERTS_CHAT_ID")
    alert_active_users_threshold: int = Field(default=100, validation_alias="ALERT_ACTIVE_USERS_THRESHOLD")
    alert_avg_time_change_percent: float = Field(default=30.0, validation_alias="ALERT_AVG_TIME_CHANGE_PERCENT")
    alert_cheat_rate_per_hour: int = Field(default=5, validation_alias="ALERT_CHEAT_RATE_PER_HOUR")

    # API Ключ Погоды (OpenWeatherMap)
    weather_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="WEATHER_API_KEY")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: Any) -> List[int]:
        """
        Преобразует строку окружения (например, JSON-список '[123, 456]' или строку через запятую '123,456')
        в валидный список целых чисел (List[int]).
        """
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    pass
            return [int(x.strip()) for x in v.split(",") if x.strip().isdigit()]
        if isinstance(v, list):
            return [int(x) for x in v]
        return []

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


class Settings(BaseSettings):
    """
    Единая точка сборки конфигурации приложения.
    """
    bot: BotConfig
    db: DbConfig
    redis: RedisConfig

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


# Инициализируем настройки. Pydantic автоматически подтянет переменные из окружения и .env
settings = Settings(
    bot=BotConfig(),
    db=DbConfig(),
    redis=RedisConfig()
)
