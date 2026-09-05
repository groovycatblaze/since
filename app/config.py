"""Configuration. One place, loaded from .env, validated at import."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
FIXTURES_DIR = DATA_DIR / "fixtures"


class Settings(BaseSettings):
    database_url: str = "postgresql://since:since_dev_password@localhost:5433/since"
    redis_url: str = "redis://localhost:6380/0"

    # LIVE reads from the provider; REPLAY feeds recorded fixtures through the
    # identical pipeline. Same code path either way — only the source swaps.
    data_mode: str = "REPLAY"

    market_timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"

    # --- baseline parameters ------------------------------------------------
    # Trailing window for mu and sigma. Long enough to be stable, short enough
    # to still reflect the stock's current behaviour.
    baseline_window_days: int = 30

    # Sigma floor. An illiquid stock whose price barely moves would otherwise
    # give a near-zero denominator and produce enormous z-scores, flagging
    # everything. 0.0001 in log terms is ~0.01% daily movement.
    sigma_floor: float = 0.0001

    # --- attention thresholds -----------------------------------------------
    # Deliberately conservative. If everything is flagged, nothing is.
    sigma_needs_attention: float = 2.0
    sigma_changed: float = 1.5
    volume_confirm_ratio: float = 1.5
    volume_alone_ratio: float = 3.0

    # Beyond this, sqrt(t) scaling of daily sigma stops being defensible and
    # the UI falls back to plain absolute change, and says so.
    max_sigma_scaling_days: int = 10

    class Config:
        env_file = PROJECT_ROOT / ".env"
        env_file_encoding = "utf-8"


settings = Settings()
