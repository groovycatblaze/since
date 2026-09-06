"""
recorder.py — captures real NSE market data to disk.

Two modes:
    python recorder.py history   # one-shot: 90 days of daily bars, for baselines
    python recorder.py record    # loop: intraday quote snapshots every 30s

Why this exists
---------------
The build window spans a weekend. Live market data is only available for a few
hours on Friday and ~2 hours on Monday morning. Everything recorded here
becomes the fixture set that the ReplayProvider feeds through the real
pipeline, so the system can be built, tested and demoed against genuine
NSE data while the market is shut.

Design notes
------------
- One HTTP request per poll for ALL symbols (yfinance batches internally).
  This mirrors the production scaling rule: fetch is keyed by instrument,
  never by user.
- Failures are recorded, not swallowed. A fixture set that contains real
  provider gaps is what makes the degraded-mode tests honest.
- Append-only JSONL, flushed every write. A crash loses at most one poll.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

# --- configuration ----------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FIXTURES_DIR = DATA_DIR / "fixtures"
HISTORY_DIR = DATA_DIR / "history"

POLL_SECONDS = 30
HISTORY_DAYS = "90d"

# Deliberately mixed: mega-cap low-vol, mid-cap, high-beta, and known movers.
# The demo needs genuine variance in volatility, otherwise sigma-normalisation
# has nothing to show.
SYMBOLS = [
    # large, low volatility
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ITC.NS",
    "HINDUNILVR.NS", "ICICIBANK.NS", "LT.NS", "KOTAKBANK.NS", "SBIN.NS",
    # large, higher beta
    "TMPV.NS", "ADANIENT.NS", "BAJFINANCE.NS", "TATASTEEL.NS",
    "JSWSTEEL.NS", "HINDALCO.NS", "AXISBANK.NS", "MARUTI.NS",
    # mid cap
    "ETERNAL.NS", "PAYTM.NS", "IRCTC.NS", "IDEA.NS", "YESBANK.NS",
    "PNB.NS", "BANKBARODA.NS", "TATAPOWER.NS", "SUZLON.NS", "IEX.NS",
    # sector spread
    "SUNPHARMA.NS", "DRREDDY.NS", "ASIANPAINT.NS", "TITAN.NS",
]

# The market benchmark. Required for the market-adjusted signal:
# if the index moved, an individual stock moving with it is not news.
INDEX_SYMBOL = "^NSEI"

ALL_TICKERS = SYMBOLS + [INDEX_SYMBOL]

_running = True


def _stop(signum, frame):
    global _running
    _running = False
    print("\n[recorder] stop requested, finishing current poll...", flush=True)


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


# --- helpers ----------------------------------------------------------------

def _now_iso() -> str:
    """Our own clock, UTC. Distinct from the exchange timestamp on purpose:
    freshness is (our clock - exchange clock), so we must record both."""
    return datetime.now(timezone.utc).isoformat()


def _write_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        fh.flush()


def _extract_last_bar(frame: pd.DataFrame, ticker: str) -> dict | None:
    """Pull the most recent complete 1-minute bar for one ticker.

    Returns None if the provider gave us nothing usable — the caller records
    that as an explicit failure rather than inventing a price.
    """
    try:
        if isinstance(frame.columns, pd.MultiIndex):
            if ticker not in frame.columns.get_level_values(0):
                return None
            sub = frame[ticker]
        else:
            sub = frame

        sub = sub.dropna(subset=["Close"])
        if sub.empty:
            return None

        last = sub.iloc[-1]
        ts = sub.index[-1]

        # Cumulative traded volume so far today = sum of the 1m bar volumes.
        # This is what we compare against median daily volume.
        cumulative_volume = float(sub["Volume"].fillna(0).sum())

        return {
            "price": float(last["Close"]),
            "open": float(sub.iloc[0]["Open"]),
            "high": float(sub["High"].max()),
            "low": float(sub["Low"].min()),
            "bar_volume": float(last["Volume"]) if pd.notna(last["Volume"]) else 0.0,
            "cumulative_volume": cumulative_volume,
            "exchange_time": ts.isoformat(),
            "bars_today": int(len(sub)),
        }
    except Exception as exc:  # noqa: BLE001 - never let one symbol kill the poll
        print(f"[recorder] parse failed for {ticker}: {exc}", flush=True)
        return None


# --- modes ------------------------------------------------------------------

def fetch_history() -> None:
    """One-shot: 90 days of daily bars per symbol.

    This is the input to the baselines: mu, robust sigma of log returns,
    median daily volume, and the 52-week range. Fetched once because it
    only changes after the close.
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[recorder] fetching {HISTORY_DAYS} of daily bars for "
          f"{len(ALL_TICKERS)} tickers...", flush=True)

    frame = yf.download(
        tickers=" ".join(ALL_TICKERS),
        period=HISTORY_DAYS,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,   # we want RAW prices: split artefacts are a
        progress=False,      # signal we explicitly want to detect later
        threads=True,
    )

    ok, failed = 0, []
    for ticker in ALL_TICKERS:
        try:
            sub = frame[ticker] if isinstance(frame.columns, pd.MultiIndex) else frame
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                failed.append(ticker)
                continue

            bars = [
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]) if pd.notna(row["Volume"]) else 0.0,
                }
                for idx, row in sub.iterrows()
            ]

            out = HISTORY_DIR / f"{ticker.replace('^', 'INDEX_')}.json"
            out.write_text(json.dumps({
                "ticker": ticker,
                "fetched_at": _now_iso(),
                "interval": "1d",
                "auto_adjust": False,
                "bars": bars,
            }, indent=2), encoding="utf-8")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[recorder] history failed for {ticker}: {exc}", flush=True)
            failed.append(ticker)

    print(f"[recorder] history done: {ok} ok, {len(failed)} failed -> {HISTORY_DIR}",
          flush=True)
    if failed:
        print(f"[recorder] failed tickers: {', '.join(failed)}", flush=True)


def fetch_session() -> None:
    """One-shot: the FULL day of 1-minute bars for every ticker.

    yfinance retains 1m bars for ~30 days, so this works after the close and
    over the weekend. This is the safety net: even if live polling captured
    nothing, the whole session can be replayed from here at any speed.

    Note the difference from record_quotes(): these bars carry the exchange
    clock only. They cannot tell us how long OUR fetch took or when the
    provider failed, which is why live polling is still worth doing.
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = FIXTURES_DIR / f"session-{day}.jsonl"
    print(f"[recorder] pulling full 1m session for {len(ALL_TICKERS)} tickers "
          f"-> {out_path}", flush=True)

    # 5d, not 1d: on a weekend or holiday "1d" can return an empty frame.
    # 1m bars are retained by the provider for ~30 days, so asking for a
    # wider window and filtering to complete sessions is strictly safer.
    frame = yf.download(
        tickers=" ".join(ALL_TICKERS),
        period="5d",
        interval="1m",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )

    ok, bars_written = 0, 0
    for ticker in ALL_TICKERS:
        try:
            sub = frame[ticker] if isinstance(frame.columns, pd.MultiIndex) else frame
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                _write_jsonl(out_path, {"ticker": ticker, "status": "NO_DATA",
                                        "fetched_at": _now_iso()})
                continue

            # Reset per session. Volume is a DAILY quantity compared against a
            # daily median; letting it run across the whole fixture window made
            # every stock look like it was trading at 5x normal by Friday.
            cumulative = 0.0
            current_session = None
            for ts, row in sub.iterrows():
                if ts.date() != current_session:
                    current_session = ts.date()
                    cumulative = 0.0
                vol = float(row["Volume"]) if pd.notna(row["Volume"]) else 0.0
                cumulative += vol
                _write_jsonl(out_path, {
                    "ticker": ticker,
                    "status": "OK",
                    "source": "yfinance",
                    "fetched_at": _now_iso(),
                    "session_date": ts.strftime("%Y-%m-%d"),
                    "exchange_time": ts.isoformat(),
                    "price": float(row["Close"]),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "bar_volume": vol,
                    "cumulative_volume": cumulative,
                })
                bars_written += 1
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[recorder] session failed for {ticker}: {exc}", flush=True)

    print(f"[recorder] session done: {ok}/{len(ALL_TICKERS)} tickers, "
          f"{bars_written} bars -> {out_path}", flush=True)


def record_quotes() -> None:
    """Loop: snapshot every symbol every POLL_SECONDS until stopped.

    Every poll writes one line per ticker, including failures. The resulting
    JSONL is replayed later by ReplayProvider through the identical pipeline.
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = FIXTURES_DIR / f"quotes-{day}.jsonl"
    print(f"[recorder] recording {len(ALL_TICKERS)} tickers every "
          f"{POLL_SECONDS}s -> {out_path}", flush=True)
    print("[recorder] Ctrl-C to stop cleanly.", flush=True)

    poll = 0
    while _running:
        started = time.monotonic()
        poll += 1
        fetched_at = _now_iso()

        try:
            frame = yf.download(
                tickers=" ".join(ALL_TICKERS),
                period="1d",
                interval="1m",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
        except Exception as exc:  # noqa: BLE001
            # A whole-batch failure is itself a valuable fixture: it is exactly
            # what the UNAVAILABLE freshness state must handle.
            print(f"[recorder] poll {poll} FAILED: {exc}", flush=True)
            _write_jsonl(out_path, {
                "poll": poll,
                "fetched_at": fetched_at,
                "ticker": None,
                "status": "BATCH_FAILURE",
                "error": str(exc)[:300],
            })
            time.sleep(POLL_SECONDS)
            continue

        ok = 0
        for ticker in ALL_TICKERS:
            bar = _extract_last_bar(frame, ticker)
            if bar is None:
                _write_jsonl(out_path, {
                    "poll": poll,
                    "fetched_at": fetched_at,
                    "ticker": ticker,
                    "status": "NO_DATA",
                })
                continue
            _write_jsonl(out_path, {
                "poll": poll,
                "fetched_at": fetched_at,
                "ticker": ticker,
                "status": "OK",
                "source": "yfinance",
                **bar,
            })
            ok += 1

        elapsed = time.monotonic() - started
        print(f"[recorder] poll {poll:>4}  ok={ok}/{len(ALL_TICKERS)}  "
              f"{elapsed:.1f}s", flush=True)

        # Sleep the remainder of the interval, not the full interval, so poll
        # cadence stays stable even when the provider is slow.
        time.sleep(max(0.0, POLL_SECONDS - elapsed))

    print(f"[recorder] stopped after {poll} polls. Fixtures: {out_path}", flush=True)


# --- entrypoint -------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "history":
        fetch_history()
    elif mode == "session":
        fetch_session()
    elif mode == "record":
        record_quotes()
    else:
        print(__doc__)
        print("Usage: python recorder.py [history|session|record]")
        sys.exit(1)
