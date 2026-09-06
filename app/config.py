"""Configuration. One place, loaded from .env, validated at import."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Full recordings are gitignored (tens of MB). data/demo/ holds a trimmed,
# committed copy so a fresh clone and a deployment both come up working.
# Prefer a local recording when one exists; fall back to the committed demo
# set otherwise. Same real NSE bars either way -- only the sampling rate
# differs, and nothing is fabricated.
_LOCAL_HISTORY = DATA_DIR / "history"
_LOCAL_FIXTURES = DATA_DIR / "fixtures"
_DEMO_HISTORY = DATA_DIR / "demo" / "history"
_DEMO_FIXTURES = DATA_DIR / "demo" / "fixtures"

HISTORY_DIR = _LOCAL_HISTORY if any(_LOCAL_HISTORY.glob("*.json")) else _DEMO_HISTORY
FIXTURES_DIR = (_LOCAL_FIXTURES if any(_LOCAL_FIXTURES.glob("session-*.jsonl"))
                else _DEMO_FIXTURES)


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
    # These MUST match the defaults in scoring.Thresholds. tests/test_scoring.py
    # asserts they do -- having the same constant defined in two places is how
    # the app ended up scoring at 1.0 while every test passed at 1.5.
    sigma_needs_attention: float = 2.0
    sigma_changed: float = 1.5
    volume_confirm_ratio: float = 1.5
    volume_alone_ratio: float = 3.0

    # Beyond this, sqrt(t) scaling of daily sigma stops being defensible and
    # the UI falls back to plain absolute change, and says so.
    max_sigma_scaling_days: int = 10

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
