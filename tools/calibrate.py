"""
calibrate.py — does the signal actually fire at a sensible rate?

Run:
    python -m tools.calibrate

Why this exists
---------------
A threshold nobody tested is a guess. If "NEEDS ATTENTION" fires on 30% of
stock-days, the product is noise wearing a suit; if it fires on 0.2%, it is
decorative. The only way to know is to walk the recorded history day by day
and count.

This is also the answer to the hardest question a reviewer can ask about the
scoring model: "how do you know your thresholds are right?" The answer should
be a measured firing rate on real NSE data, not an opinion.

Method
------
Strict walk-forward. For each day t, the baseline is computed from the 30 days
BEFORE t, then day t's return is scored against it. No future data ever enters
a baseline, so the numbers here are what the live system would actually have
produced.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import HISTORY_DIR, settings  # noqa: E402
from tools.seed import robust_sigma  # noqa: E402

WARMUP = settings.baseline_window_days


def plain_sigma(returns: list[float]) -> float:
    """Ordinary standard deviation, for comparison against the robust one."""
    if len(returns) < 2:
        return settings.sigma_floor
    return max(statistics.stdev(returns), settings.sigma_floor)


def tier_for(z: float, volume_ratio: float | None) -> str:
    """The tiering rule under test. Mirrors the production rule exactly."""
    az = abs(z)
    vr = volume_ratio or 1.0

    if az >= settings.sigma_needs_attention and vr >= settings.volume_confirm_ratio:
        return "NEEDS_ATTENTION"
    if az >= settings.sigma_changed:
        return "CHANGED"
    if vr >= settings.volume_alone_ratio and az >= 0.5:
        return "CHANGED"
    return "QUIET"


def analyse(bars: list[dict], sigma_fn) -> dict:
    """Walk forward through one instrument's history, scoring each day."""
    bars = [b for b in bars if b.get("close") and b["close"] > 0]
    if len(bars) < WARMUP + 5:
        return {}

    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    tiers = {"NEEDS_ATTENTION": 0, "CHANGED": 0, "QUIET": 0}
    z_values, biggest = [], (0.0, None, 0.0)

    # i indexes log_returns; the baseline uses only returns strictly before i.
    for i in range(WARMUP, len(log_returns)):
        window = log_returns[i - WARMUP:i]
        sigma = sigma_fn(window)
        mu = statistics.mean(window)

        z = (log_returns[i] - mu) / sigma

        vol_window = [v for v in volumes[max(0, i - 19):i + 1] if v > 0]
        median_vol = statistics.median(vol_window) if vol_window else 0
        vol_ratio = (volumes[i + 1] / median_vol) if median_vol > 0 else 1.0

        tiers[tier_for(z, vol_ratio)] += 1
        z_values.append(abs(z))

        pct = (math.exp(log_returns[i]) - 1) * 100
        if abs(z) > abs(biggest[0]):
            biggest = (z, bars[i + 1]["date"], pct)

    total = sum(tiers.values())
    return {
        "total_days": total,
        "tiers": tiers,
        "needs_pct": 100 * tiers["NEEDS_ATTENTION"] / total,
        "changed_pct": 100 * tiers["CHANGED"] / total,
        "quiet_pct": 100 * tiers["QUIET"] / total,
        "median_abs_z": statistics.median(z_values),
        "p95_abs_z": sorted(z_values)[int(0.95 * len(z_values))],
        "biggest": biggest,
    }


def main() -> None:
    files = sorted(HISTORY_DIR.glob("*.json"))
    if not files:
        print(f"No history in {HISTORY_DIR}")
        sys.exit(1)

    results = {}
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        symbol = payload["ticker"].replace(".NS", "").replace("^", "")
        if payload["ticker"].startswith("^"):
            continue  # the index is a baseline input, not a watchlist item

        robust = analyse(payload["bars"], robust_sigma)
        plain = analyse(payload["bars"], plain_sigma)
        if robust:
            results[symbol] = (robust, plain)

    print("=" * 78)
    print("CALIBRATION — walk-forward over recorded NSE history")
    print("=" * 78)

    print(f"\n{'Symbol':<13}{'ATTN%':>7}{'CHG%':>7}{'QUIET%':>8}"
          f"{'med|z|':>8}{'p95|z|':>8}   biggest move")
    print("-" * 78)

    for sym, (r, _) in sorted(results.items(), key=lambda kv: -kv[1][0]["needs_pct"]):
        z, day, pct = r["biggest"]
        print(f"{sym:<13}{r['needs_pct']:>6.1f}%{r['changed_pct']:>6.1f}%"
              f"{r['quiet_pct']:>7.1f}%{r['median_abs_z']:>8.2f}{r['p95_abs_z']:>8.2f}"
              f"   {pct:+6.1f}% z={z:+5.1f} {day}")

    agg_needs = statistics.mean(r["needs_pct"] for r, _ in results.values())
    agg_changed = statistics.mean(r["changed_pct"] for r, _ in results.values())
    agg_quiet = statistics.mean(r["quiet_pct"] for r, _ in results.values())
    med_z = statistics.mean(r["median_abs_z"] for r, _ in results.values())

    print("-" * 78)
    print(f"{'AVERAGE':<13}{agg_needs:>6.1f}%{agg_changed:>6.1f}%{agg_quiet:>7.1f}%"
          f"{med_z:>8.2f}")

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)

    # A well-calibrated z-score has median |z| near 0.67 (the normal
    # distribution's quartile). Far above means sigma is too small and
    # everything looks dramatic; far below means sigma is too large and
    # nothing ever surfaces.
    print(f"\nMedian |z| = {med_z:.2f}   (well-calibrated is roughly 0.6-0.8)")
    if med_z > 1.0:
        print("  -> TOO HIGH. Sigma is underestimating real volatility, so ordinary")
        print("     days look extraordinary. Expect false urgency.")
    elif med_z < 0.4:
        print("  -> TOO LOW. Sigma is overestimating, so real events get buried.")
    else:
        print("  -> Reasonable. The z-score is behaving like a z-score.")

    print(f"\nNEEDS_ATTENTION fires on {agg_needs:.1f}% of stock-days.")
    if agg_needs > 15:
        print("  -> TOO NOISY. On a 20-stock watchlist that is ~3 alerts every day.")
        print("     Raise sigma_needs_attention in app/config.py.")
    elif agg_needs < 1:
        print("  -> TOO QUIET. The product would rarely say anything.")
        print("     Lower sigma_needs_attention in app/config.py.")
    else:
        print(f"  -> Usable. On a 20-stock watchlist that is about "
              f"{20 * agg_needs / 100:.1f} items flagged per day.")

    # The robust-vs-plain comparison: does MAD compress burst-y stocks?
    print("\n" + "=" * 78)
    print("ROBUST (MAD) vs PLAIN (stdev) — firing rate per instrument")
    print("=" * 78)
    print(f"\n{'Symbol':<13}{'MAD ATTN%':>11}{'STD ATTN%':>11}{'diff':>9}")
    print("-" * 46)

    diffs = []
    for sym, (r, p) in sorted(results.items(),
                              key=lambda kv: -(kv[1][0]["needs_pct"] - kv[1][1]["needs_pct"])):
        d = r["needs_pct"] - p["needs_pct"]
        diffs.append(d)
        print(f"{sym:<13}{r['needs_pct']:>10.1f}%{p['needs_pct']:>10.1f}%{d:>+8.1f}")

    print("-" * 46)
    print(f"\nMean difference: {statistics.mean(diffs):+.1f} percentage points")
    print("\nIf MAD fires much MORE than stdev on volatile names, MAD is")
    print("compressing their sigma and the robustness is costing calibration.")


if __name__ == "__main__":
    main()
