"""
service.py — orchestration between the provider, the database and scoring.

The scaling idea, in one place
------------------------------
Market state is SHARED; memory is PERSONAL.

`refresh` is keyed by INSTRUMENT and never by user, so ten thousand users
watching RELIANCE cost one upstream call, not ten thousand. `build_digest`
is the only per-user work, and it is pure computation over rows already
fetched. That split is why the read path does not get more expensive as
users are added -- only as distinct instruments are.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from app.config import settings
from app.db import get_watchlist, record_observations, resolve_symbol
from app.market_calendar import IST, TradingCalendar, resolve_freshness
from app.providers import Provider, Quote
from app.scoring import (
    Baseline, Freshness, Observation, Score, Thresholds, Tier, UserContext, score,
)

log = logging.getLogger(__name__)

THRESHOLDS = Thresholds(
    sigma_needs_attention=settings.sigma_needs_attention,
    sigma_changed=settings.sigma_changed,
    volume_confirm_ratio=settings.volume_confirm_ratio,
    volume_alone_ratio=settings.volume_alone_ratio,
    max_sigma_scaling_days=settings.max_sigma_scaling_days,
)

# Do not re-fetch an instrument more often than this. The cache is keyed by
# instrument, so it deduplicates across ALL users automatically.
REFRESH_TTL_SECONDS = 30

_last_refresh: dict[str, datetime] = {}


def _needs_refresh(symbol: str, now: datetime) -> bool:
    """Has this instrument been fetched recently enough to skip?

    The obvious version is `now - last >= TTL`, and it is wrong here. The clock
    is user-controlled in replay mode, so it moves BACKWARDS as well as
    forwards. Viewing the latest data and then stepping back four days gives a
    difference of minus four days, which is not >= 30 seconds, so the cache
    concluded the data was fresh and served nothing at all. The screen said
    "no market data available yet" while 233,301 bars sat in memory.

    A cache keyed on elapsed time quietly assumes time is monotonic. Any jump
    backwards -- a replay clock, a clock correction, a leap second -- breaks
    that assumption. Comparing the elapsed time as a magnitude does not.
    """
    last = _last_refresh.get(symbol)
    if last is None:
        return True
    delta = (now - last).total_seconds()
    # Refetch on any backwards jump, and on any forward gap past the TTL.
    return delta < 0 or delta >= REFRESH_TTL_SECONDS
# One in-flight refresh at a time. Without this, twenty concurrent requests on
# a cold cache all miss together and fire twenty identical upstream calls --
# the thundering herd, and the fastest way to get rate-limited by a provider.
_refresh_lock = threading.Lock()


class MarketService:
    def __init__(self, provider: Provider, calendar: TradingCalendar):
        self.provider = provider
        self.calendar = calendar

    # -- shared half --------------------------------------------------------

    def refresh(self, symbol_to_instrument: dict[str, int],
                now: datetime) -> dict[str, Quote]:
        """Fetch quotes for these instruments and persist them.

        Returns whatever the provider gave, including failures. Callers fall
        back to the last stored observation, so a provider outage delays
        information -- it never destroys it.
        """
        with _refresh_lock:
            stale = [s for s in symbol_to_instrument
                     if _needs_refresh(s, now)]
            if not stale:
                return {}

            try:
                quotes = self.provider.fetch(stale, now)
            except Exception as exc:                      # noqa: BLE001
                # Defence in depth. Providers promise not to raise; this
                # ensures a misbehaving one degrades the request rather than
                # returning a 500 to the user.
                log.warning("provider raised despite contract: %s", exc)
                return {}

            rows = []
            for symbol, q in quotes.items():
                if not q.ok:
                    continue
                rows.append({
                    "instrument_id": symbol_to_instrument[symbol],
                    "exchange_time": q.exchange_time,
                    "fetched_at": q.fetched_at,
                    "source": q.source,
                    "status": q.status,
                    "price": q.price,
                    "cumulative_volume": q.cumulative_volume,
                    "session_date": q.exchange_time.astimezone(IST).date(),
                })
                _last_refresh[symbol] = now

            if rows:
                record_observations(rows)
            return quotes

    # -- personal half ------------------------------------------------------

    def build_digest(self, user_id: int, watchlist_id: int,
                     now: datetime) -> dict:
        rows = get_watchlist(watchlist_id, user_id, now)
        if not rows:
            return {"as_of": now.isoformat(), "tiers": _empty_tiers(),
                    "counts": {t.value: 0 for t in Tier}, "total": 0}

        symbol_to_instrument = {r["symbol"]: r["instrument_id"] for r in rows}
        self.refresh(symbol_to_instrument, now)

        # Re-read after refresh so we score the freshest stored observation
        # rather than the provider response, keeping one source of truth.
        rows = get_watchlist(watchlist_id, user_id, now)

        items = [self._score_row(r, now) for r in rows]

        tiers = _empty_tiers()
        for item in items:
            tiers[item["tier"]].append(item)

        # Within a tier, biggest surprise first. Items with no z (long
        # absences, suppressed corporate actions) sort last rather than
        # crashing the comparison.
        for bucket in tiers.values():
            bucket.sort(key=lambda i: abs(i["z"]) if i["z"] is not None else -1,
                        reverse=True)

        return {
            "as_of": now.isoformat(),
            "tiers": tiers,
            "counts": {k: len(v) for k, v in tiers.items()},
            "total": len(items),
        }

    def _score_row(self, row: dict, now: datetime) -> dict:
        symbol = row["symbol"]

        if row["price"] is None or row["prev_close"] is None:
            # No observation or no baseline. Say so rather than guessing.
            return _item(row, Score(
                tier=Tier.QUIET, scored=False,
                freshness=Freshness.UNAVAILABLE,
                reasons=[], ), symbol, note="No market data available yet.")

        fetched_at = row["fetched_at"]
        freshness = resolve_freshness(fetched_at, now, self.calendar)

        baseline = Baseline(
            prev_close=float(row["prev_close"]),
            mean_log_return=float(row["mean_log_return"]),
            sigma_log_return=float(row["sigma_log_return"]),
            median_volume=float(row["median_volume_20d"])
                if row["median_volume_20d"] else None,
            range_high=float(row["range_high"]) if row["range_high"] else None,
            range_low=float(row["range_low"]) if row["range_low"] else None,
            range_days=row["range_days"],
        )

        # How far into its own trading session this observation sits, so the
        # volume comparison is like-for-like regardless of time of day.
        bar_time = row["exchange_time"].astimezone(IST)
        session_open = bar_time.replace(hour=9, minute=15, second=0, microsecond=0)
        session_fraction = self.calendar.trading_days_between(session_open, bar_time)

        obs = Observation(
            price=float(row["price"]),
            cumulative_volume=float(row["cumulative_volume"])
                if row["cumulative_volume"] else None,
            freshness=freshness,
            session_fraction=min(max(session_fraction, 0.0), 1.0) or 1.0,
        )

        acknowledged_at = row["acknowledged_at"]
        # Added after the last acknowledgement -> genuinely new to the user,
        # even if a stale watermark row survives from a previous stint.
        is_new = acknowledged_at is None or row["added_at"] > acknowledged_at

        user = UserContext(
            watermark_price=float(row["baseline_price"])
                if row["baseline_price"] and not is_new else None,
            elapsed_trading_days=self.calendar.trading_days_between(
                acknowledged_at, now) if acknowledged_at else 1.0,
            threshold_price=float(row["threshold_price"])
                if row["threshold_price"] else None,
            is_new=is_new,
        )

        # The price path SINCE THE USER LAST LOOKED. Not since the open, not
        # the last N days -- the exact window they missed. Drawing the window
        # they were away for is what makes "memory" visible instead of being
        # something a reviewer has to read about.
        spark_from = acknowledged_at or row["added_at"]
        spark = self.provider.history(symbol, spark_from, now)

        return _item(row, score(obs, baseline, user, THRESHOLDS), symbol,
                     spark=spark)


def _empty_tiers() -> dict[str, list]:
    return {t.value: [] for t in Tier}


def _item(row: dict, s: Score, symbol: str, note: str | None = None,
          spark: list[float] | None = None) -> dict:
    """Shape the API response.

    Reasons are emitted as structured objects with the message already
    written. The frontend renders them and never reconstructs an explanation
    from raw numbers, so the rule and its wording cannot drift apart.
    """
    return {
        "instrument_id": row["instrument_id"],
        "symbol": symbol,
        "name": row["display_name"],
        "price": float(row["price"]) if row["price"] else None,
        "tier": s.tier.value,
        "z": round(s.z, 2) if s.z is not None else None,
        "pct_change": round(s.pct_change, 2) if s.pct_change is not None else None,
        "volume_ratio": round(s.volume_ratio, 2)
            if s.volume_ratio is not None else None,
        "comparison": s.comparison,
        "freshness": s.freshness.value,
        "scored": s.scored,
        "as_of": row["exchange_time"].isoformat() if row["exchange_time"] else None,
        # Rounded before serialising: four decimals of a rupee carry no
        # information for a 190px line and inflate every payload.
        "spark": [round(p, 2) for p in spark] if spark else [],
        "threshold_price": float(row["threshold_price"])
            if row["threshold_price"] else None,
        "reasons": [
            {"type": r.type.value, "message": r.message}
            for r in s.reasons
        ] or ([{"type": "DATA_UNRELIABLE", "message": note}] if note else []),
    }


def resolve_or_fail(symbol: str) -> dict:
    found = resolve_symbol(symbol)
    if not found:
        raise ValueError(f"Unknown symbol: {symbol}")
    return found
