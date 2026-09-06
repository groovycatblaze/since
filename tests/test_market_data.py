"""Tests for the market calendar, freshness resolution and replay determinism."""

import json
from datetime import date, datetime, timedelta

import pytest

from app.market_calendar import (
    IST, TradingCalendar, resolve_freshness,
)
from app.providers import ReplayProvider
from app.scoring import Freshness


def d(y, m, day, h=0, mi=0):
    return datetime(y, m, day, h, mi, tzinfo=IST)


# Fri 4 Sep 2026 and the surrounding week, minus a fabricated Wed holiday.
WEEK = {date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 4)}
CAL = TradingCalendar(WEEK)


# ---------------------------------------------------------------------------
# Trading days
# ---------------------------------------------------------------------------

def test_observed_trading_days_are_recognised():
    assert CAL.is_trading_day(date(2026, 9, 4))


def test_gap_inside_the_recorded_window_is_a_holiday():
    """2 Sep is a Wednesday with no bar, so it was a holiday. Not a weekday
    guess -- an observation."""
    assert date(2026, 9, 2).weekday() < 5
    assert not CAL.is_trading_day(date(2026, 9, 2))


def test_weekend_is_never_a_trading_day():
    assert not CAL.is_trading_day(date(2026, 9, 5))   # Saturday
    assert not CAL.is_trading_day(date(2026, 9, 6))   # Sunday


def test_outside_recorded_window_falls_back_to_weekday():
    assert CAL.is_trading_day(date(2027, 3, 15))      # a Monday
    assert not CAL.is_trading_day(date(2027, 3, 14))  # a Sunday


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("moment,expected", [
    (d(2026, 9, 4, 9, 14), False),   # one minute before the open
    (d(2026, 9, 4, 9, 15), True),    # the open
    (d(2026, 9, 4, 12, 0), True),    # midday
    (d(2026, 9, 4, 15, 30), True),   # the close
    (d(2026, 9, 4, 15, 31), False),  # one minute after
    (d(2026, 9, 5, 12, 0), False),   # Saturday midday
])
def test_market_hours(moment, expected):
    assert CAL.is_market_open(moment) is expected


# ---------------------------------------------------------------------------
# Elapsed trading time -- the weekend problem
# ---------------------------------------------------------------------------

def test_weekend_does_not_count_as_trading_days():
    """Friday close to Monday open is 3 calendar days and ~0 trading days.
    Scaling sigma by sqrt(3) here would understate every surprise by 70%."""
    elapsed = CAL.trading_days_between(d(2026, 9, 4, 15, 30), d(2026, 9, 5, 12, 0))
    assert elapsed == pytest.approx(0.0, abs=0.01)


def test_partial_session_counts_as_a_fraction():
    """09:15 to 12:22 is roughly half a session, not a whole day and not zero."""
    elapsed = CAL.trading_days_between(d(2026, 9, 4, 9, 15), d(2026, 9, 4, 12, 22))
    assert 0.45 < elapsed < 0.55


def test_full_session_is_one_day():
    elapsed = CAL.trading_days_between(d(2026, 9, 4, 9, 15), d(2026, 9, 4, 15, 30))
    assert elapsed == pytest.approx(1.0, abs=0.01)


def test_holiday_is_skipped_when_counting():
    """1 Sep to 3 Sep spans a holiday, so it is 1 trading day, not 2."""
    elapsed = CAL.trading_days_between(d(2026, 9, 1, 15, 30), d(2026, 9, 3, 15, 30))
    assert elapsed == pytest.approx(1.0, abs=0.05)


def test_backwards_range_is_zero_not_negative():
    assert CAL.trading_days_between(d(2026, 9, 4, 15, 0), d(2026, 9, 4, 9, 30)) == 0.0


# ---------------------------------------------------------------------------
# Freshness -- the closed-vs-stale distinction
# ---------------------------------------------------------------------------

def test_data_from_hours_ago_is_CLOSED_not_STALE_when_market_is_shut():
    """THE distinction most implementations get wrong. Saturday evening, the
    Friday close is the most current price that exists."""
    f = resolve_freshness(d(2026, 9, 4, 15, 30), d(2026, 9, 5, 20, 0), CAL)
    assert f is Freshness.CLOSED


def test_old_data_during_trading_hours_is_STALE():
    f = resolve_freshness(d(2026, 9, 4, 11, 0), d(2026, 9, 4, 12, 0), CAL)
    assert f is Freshness.STALE


@pytest.mark.parametrize("age_seconds,expected", [
    (10, Freshness.LIVE),
    (59, Freshness.LIVE),
    (120, Freshness.DELAYED),
    (299, Freshness.DELAYED),
    (600, Freshness.STALE),
])
def test_freshness_thresholds_during_market_hours(age_seconds, expected):
    now = d(2026, 9, 4, 12, 0)
    assert resolve_freshness(now - timedelta(seconds=age_seconds), now, CAL) is expected


def test_fetch_failure_beats_every_other_state():
    f = resolve_freshness(d(2026, 9, 4, 12, 0), d(2026, 9, 4, 12, 0), CAL,
                          fetch_failed=True)
    assert f is Freshness.UNAVAILABLE


def test_clock_skew_does_not_crash():
    """Provider timestamp ahead of ours. Degrade, do not explode."""
    now = d(2026, 9, 4, 12, 0)
    assert resolve_freshness(now + timedelta(seconds=30), now, CAL) is Freshness.LIVE


# ---------------------------------------------------------------------------
# Replay determinism
# ---------------------------------------------------------------------------

@pytest.fixture
def fixtures(tmp_path):
    path = tmp_path / "session-20260904.jsonl"
    rows = []
    for minute in range(10):
        rows.append({
            "ticker": "TEST.NS", "status": "OK", "source": "yfinance",
            "exchange_time": d(2026, 9, 4, 10, minute).isoformat(),
            "price": 100.0 + minute, "cumulative_volume": 1000.0 * (minute + 1),
        })
    rows.append({"ticker": "MISSING.NS", "status": "NO_DATA",
                 "exchange_time": d(2026, 9, 4, 10, 0).isoformat()})
    rows.append("{ this is a torn line")
    path.write_text("\n".join(
        r if isinstance(r, str) else json.dumps(r) for r in rows), encoding="utf-8")
    return tmp_path


def test_replay_never_serves_a_price_from_the_future(fixtures):
    """The clock is at 10:05, so 10:06 onward must be invisible."""
    p = ReplayProvider(fixtures)
    q = p.fetch(["TEST.NS"], d(2026, 9, 4, 10, 5))["TEST.NS"]
    assert q.price == 105.0
    assert q.exchange_time <= d(2026, 9, 4, 10, 5)


def test_replay_is_deterministic(fixtures):
    """Same clock, same answer, every time. This is what makes the demo safe."""
    p = ReplayProvider(fixtures)
    a = p.fetch(["TEST.NS"], d(2026, 9, 4, 10, 7))["TEST.NS"]
    b = p.fetch(["TEST.NS"], d(2026, 9, 4, 10, 7))["TEST.NS"]
    assert a.price == b.price == 107.0


def test_replay_advancing_the_clock_shows_movement(fixtures):
    """The returning-user scenario, in miniature."""
    p = ReplayProvider(fixtures)
    before = p.fetch(["TEST.NS"], d(2026, 9, 4, 10, 1))["TEST.NS"]
    after = p.fetch(["TEST.NS"], d(2026, 9, 4, 10, 8))["TEST.NS"]
    assert after.price > before.price


def test_replay_before_first_bar_returns_no_data_not_a_guess(fixtures):
    p = ReplayProvider(fixtures)
    q = p.fetch(["TEST.NS"], d(2026, 9, 4, 9, 30))["TEST.NS"]
    assert q.status == "NO_DATA" and q.price is None


def test_replay_unknown_symbol_returns_no_data(fixtures):
    p = ReplayProvider(fixtures)
    q = p.fetch(["NOSUCH.NS"], d(2026, 9, 4, 10, 5))["NOSUCH.NS"]
    assert q.status == "NO_DATA"
    assert not q.ok


def test_replay_skips_torn_lines_without_aborting(fixtures):
    """A truncated write must not cost us the other 59,000 bars."""
    p = ReplayProvider(fixtures)
    assert "TEST.NS" in p.symbols


def test_replay_fetched_at_is_the_replay_clock(fixtures):
    """So downstream freshness logic behaves exactly as it would live."""
    p = ReplayProvider(fixtures)
    now = d(2026, 9, 4, 10, 5)
    assert p.fetch(["TEST.NS"], now)["TEST.NS"].fetched_at == now


def test_missing_fixtures_fail_loudly(tmp_path):
    """Silently serving an empty timeline would make the demo mysteriously
    blank. Fail at startup with an actionable message instead."""
    with pytest.raises(FileNotFoundError, match="recorder.py session"):
        ReplayProvider(tmp_path)


# ---------------------------------------------------------------------------
# Timestamp parsing at the API edge
# ---------------------------------------------------------------------------

def test_mangled_timezone_offset_is_repaired():
    """A "+05:30" offset arrives as " 05:30" whenever the query string is not
    URL-encoded, because '+' is the encoding for a space. Rejecting it would
    surface as a broken clock rather than a quoting mistake, so we repair."""
    import re
    from datetime import datetime as dt

    def repair(raw: str) -> dt:
        return dt.fromisoformat(re.sub(r"\s(\d{2}:\d{2})$", r"+\1", raw.strip()))

    assert repair("2026-09-04T15:24:00 05:30") == repair("2026-09-04T15:24:00+05:30")
    assert repair("2026-09-04T15:24:00").tzinfo is None


# ---------------------------------------------------------------------------
# The refresh cache must not assume time is monotonic
# ---------------------------------------------------------------------------

def test_refresh_cache_handles_a_backwards_clock():
    """With a user-controlled clock, time moves both ways. `now - last >= TTL`
    is False for a negative delta, so stepping the clock BACK made the cache
    report stale-in-the-future data as fresh and serve nothing."""
    from app.service import _last_refresh, _needs_refresh

    sym = "TEST.NS"
    later = d(2026, 9, 4, 15, 0)
    earlier = d(2026, 8, 31, 10, 0)

    _last_refresh.clear()
    assert _needs_refresh(sym, later) is True      # never fetched
    _last_refresh[sym] = later

    # Immediately again: cached, correct.
    assert _needs_refresh(sym, later) is False
    # Forward past the TTL: refetch.
    assert _needs_refresh(sym, later + timedelta(seconds=31)) is True
    # BACKWARDS: must refetch. This is the bug.
    assert _needs_refresh(sym, earlier) is True
    _last_refresh.clear()
