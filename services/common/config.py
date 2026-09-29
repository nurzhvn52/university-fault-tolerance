"""Settings shared by all services, read from UFT_* environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UFT_", extra="ignore")

    service_name: str = "service"
    # "baseline" runs the system without fault-tolerance mechanisms, "ft" enables them.
    ft_mode: Literal["baseline", "ft"] = "baseline"
    # Simulated physical node the container belongs to (used for node-failure experiments).
    node: str = "a"
    log_level: str = "INFO"
    # Mounts the /_chaos admin endpoint used by the experiment tooling.
    chaos_enabled: bool = False
    term: str = "2026-FALL"

    database_url: str = "postgresql+asyncpg://uft:uft_local_only@localhost:5432/university"
    db_pool_size: int = 10

    student_url: str = "http://gateway:8080"
    payment_url: str = "http://gateway:8080"
    timetable_url: str = "http://gateway:8080"
    bank_url: str = "http://bank:8000"

    bank_db_path: str = "/data/bank/bank.db"
    bank_latency_ms: int = 80
    transcript_dir: str = "/data/transcripts"
    # Emulated cost of placing one section during timetable generation.
    timetable_step_delay_s: float = 0.1

    @property
    def ft(self) -> bool:
        return self.ft_mode == "ft"


@lru_cache
def get_settings() -> Settings:
    return Settings()
