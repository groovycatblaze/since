"""
db.py — connection pooling and every SQL statement in the system.

All SQL lives here. The service layer above never writes a query and the
scoring engine never sees a connection. That boundary is what keeps
scoring.py pure and therefore exhaustively testable in milliseconds.

Every query is parameterised. There is no string interpolation into SQL
anywhere in this file, which is the only real defence against injection and
matters more than usual when the product handles financial data.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

from app.config import settings

log = logging.getLogger(__name__)

# A pool, not a connection per request. Postgres forks a backend process per
# connection, so opening one per request would collapse under any concurrency
# and add handshake latency to every single call.
_pool: pg_pool.ThreadedConnectionPool | None = None


def init_pool(minconn: int = 1, maxconn: int = 10) -> None:
    global _pool
    if _pool is None:
        _pool = pg_pool.ThreadedConnectionPool(
            minconn, maxconn, settings.database_url)
        log.info("database pool ready (%d-%d connections)", minconn, maxconn)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_conn():
    """Borrow a connection, always return it.

    The finally block matters: without it, an exception mid-query leaks the
    connection and the pool is exhausted after `maxconn` errors. That failure
    looks like the app hanging rather than erroring, which makes it painful
    to diagnose in production.
    """
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


@contextmanager
def get_cursor():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def ensure_user(email: str) -> int:
    """Idempotent. ON CONFLICT rather than SELECT-then-INSERT, which would
    race two concurrent first-time logins into a duplicate key error."""
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO users (email) VALUES (%s) "
            "ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email "
            "RETURNING id",
            (email,),
        )
        return cur.fetchone()["id"]


def ensure_watchlist(user_id: int) -> int:
    with get_cursor() as cur:
        cur.execute("SELECT id FROM watchlists WHERE user_id = %s "
                    "ORDER BY id LIMIT 1", (user_id,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute("INSERT INTO watchlists (user_id) VALUES (%s) RETURNING id",
                    (user_id,))
        return cur.fetchone()["id"]


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------

def resolve_symbol(symbol: str) -> dict | None:
    """Ticker -> instrument. Goes through instrument_symbols because a ticker
    is an attribute of an instrument over time, not its identity. This is what
    survives ZOMATO becoming ETERNAL."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.display_name, s.symbol
            FROM instrument_symbols s
            JOIN instruments i ON i.id = s.instrument_id
            WHERE UPPER(s.symbol) = UPPER(%s) AND s.valid_to IS NULL
            """,
            (symbol,),
        )
        return cur.fetchone()


def search_instruments(query: str, limit: int = 10) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.display_name, s.symbol
            FROM instrument_symbols s
            JOIN instruments i ON i.id = s.instrument_id
            WHERE s.valid_to IS NULL
              AND i.is_index = FALSE
              AND (i.display_name ILIKE %s OR s.symbol ILIKE %s)
            ORDER BY i.display_name
            LIMIT %s
            """,
            (f"%{query}%", f"%{query}%", limit),
        )
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def add_item(watchlist_id: int, instrument_id: int,
             threshold: float | None = None,
             added_at: datetime | None = None) -> dict:
    """Adding the same instrument twice is a no-op, not an error.

    added_at is passed in rather than defaulting to NOW() because the system
    must have ONE clock authority. Watermarks advance on the request clock,
    so if this column used the database wall clock the two would disagree and
    every item would compare as "added after I last acknowledged" forever.
    Mixed clocks is a subtle bug class; the fix is to never mix them.

    DO UPDATE rather than DO NOTHING so the statement still RETURNS a row --
    with DO NOTHING a duplicate add returns nothing and the caller cannot tell
    success from failure.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_items
                (watchlist_id, instrument_id, threshold_price, added_at)
            VALUES (%s, %s, %s, COALESCE(%s, NOW()))
            ON CONFLICT (watchlist_id, instrument_id) DO UPDATE
                SET threshold_price = COALESCE(EXCLUDED.threshold_price,
                                               watchlist_items.threshold_price)
            RETURNING id, instrument_id, added_at, threshold_price
            """,
            (watchlist_id, instrument_id, threshold, added_at),
        )
        return dict(cur.fetchone())


def remove_item(watchlist_id: int, instrument_id: int,
                user_id: int | None = None) -> bool:
    """Remove from the watchlist and forget where the user left off.

    Dropping the watermark alongside the item is the intuitive reading of
    "remove": if you stop following something and later start again, you are
    starting fresh, not resuming a stint you ended months ago. Keeping it meant
    a re-added stock could silently compare against a price from a period the
    user was not watching at all.

    Deleting also makes the state resettable from the UI, which matters because
    the alternative was reaching into the database to demonstrate the product
    twice.
    """
    with get_cursor() as cur:
        cur.execute(
            "DELETE FROM watchlist_items WHERE watchlist_id = %s "
            "AND instrument_id = %s",
            (watchlist_id, instrument_id),
        )
        removed = cur.rowcount > 0
        if removed and user_id is not None:
            cur.execute(
                "DELETE FROM user_watermarks WHERE user_id = %s "
                "AND instrument_id = %s",
                (user_id, instrument_id),
            )
        return removed


def get_watchlist(watchlist_id: int, user_id: int,
                  as_of: datetime | None = None) -> list[dict]:
    """The watchlist joined to its shared market state and personal memory.

    One query, not N. A per-item loop would issue 20 round trips for a 20-stock
    watchlist and is the most common reason these dashboards feel slow.

    LEFT JOIN on watermarks because a never-acknowledged instrument is normal,
    not exceptional.

    Named parameters, not positional: the as_of placeholder sits inside a
    LATERAL subquery that appears EARLIER in the query text than the user_id
    it is passed after, so positional binding silently compared a user id
    against a timestamp. Named binding makes placeholder order irrelevant.

    NO LOOKAHEAD: the observation subquery is bounded by `as_of`, so the
    newest bar AT OR BEFORE the clock is chosen -- never the newest bar in the
    table. Without this bound, replaying Monday morning would happily serve
    Friday afternoon's price, and in production a clock skew or a late-arriving
    backfill would let a future price leak into a past comparison. The read
    path must not be able to see forward.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT
                wi.instrument_id,
                wi.added_at,
                wi.threshold_price,
                i.display_name,
                s.symbol,
                b.prev_close, b.mean_log_return, b.sigma_log_return,
                b.median_volume_20d, b.range_high, b.range_low, b.range_days,
                o.price, o.cumulative_volume, o.exchange_time, o.fetched_at,
                o.status AS obs_status,
                w.acknowledged_at, w.baseline_price
            FROM watchlist_items wi
            JOIN instruments i ON i.id = wi.instrument_id
            JOIN instrument_symbols s
                ON s.instrument_id = i.id AND s.valid_to IS NULL
            LEFT JOIN LATERAL (
                SELECT prev_close, mean_log_return, sigma_log_return,
                       median_volume_20d, range_high, range_low, range_days
                FROM instrument_baselines
                WHERE instrument_id = wi.instrument_id
                ORDER BY as_of_date DESC LIMIT 1
            ) b ON TRUE
            LEFT JOIN LATERAL (
                SELECT price, cumulative_volume, exchange_time, fetched_at, status
                FROM market_observations
                WHERE instrument_id = wi.instrument_id
                  AND exchange_time <= COALESCE(%(as_of)s, NOW())
                ORDER BY exchange_time DESC LIMIT 1
            ) o ON TRUE
            LEFT JOIN user_watermarks w
                ON w.instrument_id = wi.instrument_id AND w.user_id = %(user_id)s
            WHERE wi.watchlist_id = %(watchlist_id)s
            ORDER BY i.display_name
            """,
            {"user_id": user_id, "as_of": as_of,
             "watchlist_id": watchlist_id},
        )
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def record_observations(rows: list[dict]) -> int:
    """Bulk insert, ignoring duplicates.

    ON CONFLICT DO NOTHING against the natural key makes ingest idempotent:
    two workers racing on the same poll, or a retried request, cannot create
    duplicate bars. Duplicates would silently corrupt every volume figure.
    """
    if not rows:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO market_observations (
                    instrument_id, exchange_time, fetched_at, source, status,
                    price, cumulative_volume, session_date
                ) VALUES %s
                ON CONFLICT (instrument_id, exchange_time, source) DO NOTHING
                """,
                [(r["instrument_id"], r["exchange_time"], r["fetched_at"],
                  r["source"], r["status"], r.get("price"),
                  r.get("cumulative_volume"), r["session_date"]) for r in rows],
            )
            return cur.rowcount


# ---------------------------------------------------------------------------
# Watermarks -- the core of "since you last checked"
# ---------------------------------------------------------------------------

def acknowledge(user_id: int, entries: list[tuple[int, datetime, float]]) -> int:
    """Advance watermarks. MONOTONIC: they never move backwards.

    The WHERE clause on the DO UPDATE is the whole concurrency story. Two
    devices acknowledging at once both succeed; the later timestamp wins and
    the earlier one becomes a no-op. No locks, no transactions spanning
    requests, no lost updates -- and the operation is naturally idempotent,
    so a retried request is harmless.
    """
    if not entries:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO user_watermarks
                    (user_id, instrument_id, acknowledged_at, baseline_price)
                VALUES %s
                ON CONFLICT (user_id, instrument_id) DO UPDATE
                    SET acknowledged_at = EXCLUDED.acknowledged_at,
                        baseline_price  = EXCLUDED.baseline_price,
                        updated_at      = NOW()
                    WHERE user_watermarks.acknowledged_at < EXCLUDED.acknowledged_at
                """,
                [(user_id, iid, ts, price) for iid, ts, price in entries],
            )
            return cur.rowcount


def health() -> dict:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM instruments")
        instruments = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM market_observations")
        observations = cur.fetchone()["n"]
    return {"instruments": instruments, "observations": observations}
