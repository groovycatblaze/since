"""
seed.py — one-shot: turn recorded history into instruments and baselines.

Run once after `docker compose up`:
    python -m tools.seed

What it does NOT do
-------------------
It does not load the intraday fixtures into market_observations. Those stay
as files and are fed in by the poller through ReplayProvider, so that seeded
data travels the exact same code path as live data. Pre-loading them would
mean the whole week is already "observed" before the user ever looks, which
would make the returning-user demo meaningless.

What a baseline is
------------------
The denominator that turns "moved 2%" into "moved 3x more than it normally
does". Computed once per instrument per day, shared by every user watching
that instrument. This is the shared-state half of the architecture.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import HISTORY_DIR, settings  # noqa: E402

# 1.4826 makes the median absolute deviation a consistent estimator of sigma
# for normally distributed data. Standard constant, not a tuned magic number.
MAD_TO_SIGMA = 1.4826


def robust_sigma(returns: list[float]) -> float:
    """Volatility estimate from the median absolute deviation.

    Why not statistics.stdev? One earnings gap inflates a plain standard
    deviation for the entire trailing window, which quietly suppresses every
    subsequent real signal in that stock. MAD ignores the outlier instead of
    absorbing it, so the baseline reflects normal behaviour rather than the
    single most abnormal day in the window.
    """
    if len(returns) < 2:
        return settings.sigma_floor

    med = statistics.median(returns)
    mad = statistics.median([abs(r - med) for r in returns])
    sigma = MAD_TO_SIGMA * mad

    # A stock can legitimately have several identical closes, making MAD zero.
    # Fall back to stdev, then to the floor. Never return zero: it would make
    # every z-score infinite.
    if sigma <= 0:
        try:
            sigma = statistics.stdev(returns)
        except statistics.StatisticsError:
            sigma = 0.0

    return max(sigma, settings.sigma_floor)


def compute_baseline(bars: list[dict]) -> dict | None:
    """Derive one baseline row from a series of daily bars."""
    bars = [b for b in bars if b.get("close") and b["close"] > 0]
    if len(bars) < 11:  # need >=10 returns; schema enforces sample_days >= 10
        return None

    window = bars[-(settings.baseline_window_days + 1):]

    log_returns = [
        math.log(window[i]["close"] / window[i - 1]["close"])
        for i in range(1, len(window))
    ]

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars[-20:] if b.get("volume")]

    return {
        "prev_close": window[-1]["close"],
        "mean_log_return": statistics.mean(log_returns),
        "sigma_log_return": robust_sigma(log_returns),
        "median_volume_20d": statistics.median(volumes) if volumes else None,
        "range_high": max(closes),
        "range_low": min(closes),
        "range_days": len(bars),
        "sample_days": len(log_returns),
        "as_of_date": date.fromisoformat(window[-1]["date"]),
    }


def upsert_instrument(cur, symbol: str, is_index: bool) -> int:
    """Resolve a ticker to a stable instrument id, creating it if new.

    Lookup goes through instrument_symbols, not instruments, because the
    ticker is an attribute of the instrument over time rather than its
    identity. This is what survives ZOMATO -> ETERNAL.
    """
    cur.execute(
        "SELECT instrument_id FROM instrument_symbols "
        "WHERE symbol = %s AND valid_to IS NULL",
        (symbol,),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    display = symbol.replace(".NS", "").replace("^", "")
    cur.execute(
        "INSERT INTO instruments (display_name, is_index) VALUES (%s, %s) "
        "RETURNING id",
        (display, is_index),
    )
    instrument_id = cur.fetchone()[0]

    cur.execute(
        "INSERT INTO instrument_symbols (instrument_id, symbol) VALUES (%s, %s)",
        (instrument_id, symbol),
    )
    return instrument_id


def main() -> None:
    files = sorted(HISTORY_DIR.glob("*.json"))
    if not files:
        print(f"No history found in {HISTORY_DIR}. "
              f"Run: python tools\\recorder.py history")
        sys.exit(1)

    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    cur = conn.cursor()

    seeded, skipped = 0, []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        symbol = payload["ticker"]
        is_index = symbol.startswith("^")

        baseline = compute_baseline(payload["bars"])
        if baseline is None:
            skipped.append(f"{symbol} (insufficient history)")
            continue

        instrument_id = upsert_instrument(cur, symbol, is_index)

        # Idempotent: re-running seed overwrites the day's baseline rather
        # than failing or duplicating. Matters because this WILL be re-run.
        cur.execute(
            """
            INSERT INTO instrument_baselines (
                instrument_id, as_of_date, prev_close, mean_log_return,
                sigma_log_return, median_volume_20d, range_high, range_low,
                range_days, sample_days
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (instrument_id, as_of_date) DO UPDATE SET
                prev_close        = EXCLUDED.prev_close,
                mean_log_return   = EXCLUDED.mean_log_return,
                sigma_log_return  = EXCLUDED.sigma_log_return,
                median_volume_20d = EXCLUDED.median_volume_20d,
                range_high        = EXCLUDED.range_high,
                range_low         = EXCLUDED.range_low,
                range_days        = EXCLUDED.range_days,
                sample_days       = EXCLUDED.sample_days,
                computed_at       = NOW()
            """,
            (
                instrument_id, baseline["as_of_date"], baseline["prev_close"],
                baseline["mean_log_return"], baseline["sigma_log_return"],
                baseline["median_volume_20d"], baseline["range_high"],
                baseline["range_low"], baseline["range_days"],
                baseline["sample_days"],
            ),
        )
        seeded += 1

    conn.commit()

    # Show the spread of sigma. If every stock has the same volatility, the
    # normalisation is doing nothing and the whole thesis is empty — so this
    # is worth eyeballing, not just trusting.
    cur.execute(
        """
        SELECT i.display_name,
               ROUND(b.sigma_log_return * 100, 2) AS daily_sigma_pct
        FROM instrument_baselines b
        JOIN instruments i ON i.id = b.instrument_id
        ORDER BY b.sigma_log_return DESC
        """
    )
    rows = cur.fetchall()

    print(f"\nSeeded {seeded} instruments, skipped {len(skipped)}")
    for s in skipped:
        print(f"  skipped: {s}")

    print("\nDaily volatility (sigma of log returns), most to least volatile:")
    for name, sigma_pct in rows:
        bar = "#" * max(1, int(float(sigma_pct) * 6))
        print(f"  {name:<14} {sigma_pct:>5}%  {bar}")

    print("\nIf the top and bottom differ by several times, normalisation has "
          "something real to do.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
