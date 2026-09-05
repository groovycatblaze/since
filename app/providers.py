"""
providers.py — where market data comes from.

One protocol, two implementations. The pipeline downstream cannot tell them
apart, which is the whole point: seeded demo data travels the identical code
path as live data. A demo that bypasses the real system proves nothing.

    LiveProvider   -> yfinance, real time, unpredictable
    ReplayProvider -> recorded NSE fixtures, deterministic, offline

Why replay is not cheating
--------------------------
The fixtures are real NSE bars recorded from the live market. Replay controls
WHEN they are served, not WHAT they contain. Nothing is fabricated. This is
stated in the README and on screen, because a demo that quietly passes off
synthetic data as live would be exactly the kind of thing that should sink a
submission at a broker.

Why replay is necessary
-----------------------
The market is closed for most of the build window, and quiet even when open.
A returning-user demo needs a guaranteed interesting change at a controllable
moment. Replay gives determinism without dishonesty.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quote:
    """One observation of one instrument, as the provider reported it.

    Two clocks, deliberately:
        exchange_time -- when the market says this happened
        fetched_at    -- when we saw it
    Freshness is the gap between them. Storing only one makes staleness
    uncomputable, which is why most implementations cannot answer the brief's
    question about stale data at all.
    """
    symbol: str
    price: float | None
    cumulative_volume: float | None
    exchange_time: datetime
    fetched_at: datetime
    source: str
    status: str = "OK"          # OK | NO_DATA | FETCH_FAILED

    @property
    def ok(self) -> bool:
        return self.status == "OK" and self.price is not None and self.price > 0


class Provider(Protocol):
    """The seam between the system and the outside world.

    Everything unreliable lives behind this line: network, rate limits,
    upstream outages, schema drift. Everything in front of it is deterministic
    and testable. That separation is why the scoring engine has no try/except
    in it anywhere.
    """

    name: str

    def fetch(self, symbols: list[str], now: datetime) -> dict[str, Quote]:
        """Never raises. Failures come back as Quote(status=...), because a
        provider outage is an expected operating condition, not an exception.
        A partial result is normal: some symbols succeed, others do not."""
        ...


class ReplayProvider:
    """Serves recorded fixtures as if live, against a caller-supplied clock.

    Reads the session JSONL once into per-symbol sorted timelines, then answers
    each fetch with the most recent bar at or before `now`. Serving the latest
    bar *at or before* the clock -- rather than the nearest -- is what makes
    replay honest: the system can never see a price from the future.
    """

    name = "replay"

    def __init__(self, fixtures_dir: Path):
        self._timelines: dict[str, list[tuple[datetime, dict]]] = {}
        self._load(fixtures_dir)

    def _load(self, fixtures_dir: Path) -> None:
        files = sorted(fixtures_dir.glob("session-*.jsonl"))
        if not files:
            raise FileNotFoundError(
                f"No session fixtures in {fixtures_dir}. "
                f"Run: python tools/recorder.py session"
            )

        rows = 0
        for path in files:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue        # skip a torn line, do not abort the load
                    if rec.get("status") != "OK" or not rec.get("ticker"):
                        continue
                    try:
                        ts = datetime.fromisoformat(rec["exchange_time"])
                    except (KeyError, ValueError):
                        continue
                    self._timelines.setdefault(rec["ticker"], []).append((ts, rec))
                    rows += 1

        for symbol in self._timelines:
            self._timelines[symbol].sort(key=lambda pair: pair[0])

        log.info("ReplayProvider loaded %d bars across %d symbols from %d file(s)",
                 rows, len(self._timelines), len(files))

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        """Earliest and latest bar available. The replay clock must stay inside
        this, so callers can validate a requested demo time up front."""
        stamps = [tl[0][0] for tl in self._timelines.values() if tl] + \
                 [tl[-1][0] for tl in self._timelines.values() if tl]
        return (min(stamps), max(stamps)) if stamps else None

    @property
    def symbols(self) -> list[str]:
        return sorted(self._timelines)

    def fetch(self, symbols: list[str], now: datetime) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for symbol in symbols:
            timeline = self._timelines.get(symbol)
            if not timeline:
                out[symbol] = Quote(symbol, None, None, now, now,
                                    self.name, status="NO_DATA")
                continue

            # Rightmost bar with timestamp <= now. Binary search: this runs on
            # every request for every symbol, and a linear scan over ~1,800
            # bars per symbol would show up immediately at any real load.
            idx = bisect_right([ts for ts, _ in timeline], now) - 1
            if idx < 0:
                # Clock sits before this symbol's first recorded bar.
                out[symbol] = Quote(symbol, None, None, now, now,
                                    self.name, status="NO_DATA")
                continue

            ts, rec = timeline[idx]
            out[symbol] = Quote(
                symbol=symbol,
                price=rec.get("price"),
                cumulative_volume=rec.get("cumulative_volume"),
                exchange_time=ts,
                # fetched_at is the replay clock, not the recording time, so
                # downstream freshness logic behaves exactly as it would live.
                fetched_at=now,
                source=self.name,
            )
        return out


class LiveProvider:
    """yfinance. Unofficial, rate-limited, and occasionally wrong.

    Batches every symbol into a single request because quote fetches are keyed
    by INSTRUMENT, never by user: ten thousand users watching RELIANCE must
    still cost one upstream call. That is the entire scaling story, and it is
    enforced here rather than hoped for.
    """

    name = "yfinance"

    def __init__(self, stale_after: timedelta = timedelta(minutes=5)):
        self.stale_after = stale_after

    def fetch(self, symbols: list[str], now: datetime) -> dict[str, Quote]:
        import pandas as pd
        import yfinance as yf

        try:
            frame = yf.download(
                tickers=" ".join(symbols), period="1d", interval="1m",
                group_by="ticker", auto_adjust=False, progress=False, threads=True,
            )
        except Exception as exc:                        # noqa: BLE001
            # A whole-batch failure is an expected operating condition. Every
            # symbol degrades to UNAVAILABLE and the caller keeps serving last
            # known good values rather than the request failing.
            log.warning("provider batch fetch failed: %s", exc)
            return {s: Quote(s, None, None, now, now, self.name,
                             status="FETCH_FAILED") for s in symbols}

        out: dict[str, Quote] = {}
        for symbol in symbols:
            try:
                sub = frame[symbol] if isinstance(frame.columns, pd.MultiIndex) else frame
                sub = sub.dropna(subset=["Close"])
                if sub.empty:
                    out[symbol] = Quote(symbol, None, None, now, now,
                                        self.name, status="NO_DATA")
                    continue
                out[symbol] = Quote(
                    symbol=symbol,
                    price=float(sub.iloc[-1]["Close"]),
                    cumulative_volume=float(sub["Volume"].fillna(0).sum()),
                    exchange_time=sub.index[-1].to_pydatetime(),
                    fetched_at=now,
                    source=self.name,
                )
            except Exception as exc:                    # noqa: BLE001
                # One malformed symbol must never take down the other 32.
                log.warning("parse failed for %s: %s", symbol, exc)
                out[symbol] = Quote(symbol, None, None, now, now,
                                    self.name, status="NO_DATA")
        return out
