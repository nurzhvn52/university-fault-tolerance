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
    # Comma-separated directories on separate volumes; more than one means mirrored storage.
    transcript_dirs: str = ""
    # Emulated cost of placing one section during timetable generation.
    timetable_step_delay_s: float = 0.1

    # Fault-tolerance settings, used only when ft_mode == "ft".
    # Timeouts of one attempt of a call to another service.
    payment_timeout_s: float = 1.0
    timetable_timeout_s: float = 1.0
    bank_timeout_s: float = 2.0
    # Database: connect timeout, per-query timeout, wait for a free pooled connection. A
    # connection inside the Docker network takes milliseconds, so 0.5 s means "unreachable".
    db_connect_timeout_s: float = 0.5
    db_command_timeout_s: float = 2.0
    db_pool_timeout_s: float = 1.0
    # Retry with exponential backoff and full jitter.
    retry_attempts: int = 3
    retry_base_delay_s: float = 0.1
    retry_max_delay_s: float = 1.0
    # Circuit breaker: consecutive failures to open, time before a trial call.
    breaker_failures: int = 5
    breaker_reset_s: float = 5.0
    # Load shedding: requests one instance handles at once before it answers 503.
    max_in_flight: int = 48
    # Background recovery workers.
    worker_interval_s: float = 2.0
    pending_grace_s: float = 5.0
    transcript_cache_refresh_s: float = 60.0
    storage_scrub_s: float = 20.0
    timetable_checkpoint_every: int = 10

    @property
    def ft(self) -> bool:
        return self.ft_mode == "ft"


@lru_cache
def get_settings() -> Settings:
    return Settings()
