from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything tunable lives here so the worker and the API stay in sync."""

    database_url: str = "postgresql+psycopg2://postgres@localhost:5432/scheduler"

    poll_interval_seconds: float = 2.0
    batch_size: int = 10
    worker_concurrency: int = 4

    lease_seconds: int = 60
    reaper_interval_seconds: float = 10.0

    retry_backoff_seconds: int = 5
    retry_backoff_max_seconds: int = 300

    failure_rate: float = 0.3
    task_min_seconds: float = 1.0
    task_max_seconds: float = 3.0

    dead_letter_enabled: bool = True

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60

    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
