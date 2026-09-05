"""
market_calendar.py — market hours, trading days, and data freshness.

Why this file exists
--------------------
Financial data is only interpretable against a market calendar. Without one:

  - Previous close at 8pm on a Saturday gets labelled STALE, which is wrong.
    It is the most current data that exists, and calling it stale trains the
    user to distrust a correct number.
  - "Since you last checked, 3 days ago" over a weekend means ONE trading day
    of movement, not three. Scaling sigma by sqrt(3) would understate the
    surprise by 70%.

The calendar problem, and how this solves it
--------------------------------------------
Hardcoding NSE holidays is a trap: the list changes annually, and a wrong or
stale list produces silently wrong answers. So the trading calendar is DERIVED
from the recorded daily bars. A date on which the exchange published a bar was
a trading day; a date it did not was not. The calendar is therefore observed
rather than asserted, and it self-corrects whenever history is re-recorded.

Documented limitation: the derived calendar only covers the recorded window.
Outside it we fall back to a weekday test, which will treat an unrecorded
holiday as a trading day. That is a known, bounded inaccuracy -- it slightly
overstates elapsed trading days across a holiday, which makes the system
marginally more conservative, not less.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.scoring import Freshness

IST = ZoneInfo("Asia/Kolkata")

# NSE equity cash market, normal session.
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Freshness thresholds, applied only while the market is actually open.
LIVE_SECONDS = 60
DELAYED_SECONDS = 300


class TradingCalendar:
    """Trading days observed from recorded exchange data."""

    def __init__(self, trading_days: set[date] | None = None):
        self._days: set[date] = trading_days or set()

    @classmethod
    def from_history(cls, history_dir: Path) -> "TradingCalendar":
        """Every date on which any instrument published a daily bar."""
        days: set[date] = set()
        for path in history_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for bar in payload.get("bars", []):
                try:
                    days.add(date.fromisoformat(bar["date"]))
                except (KeyError, ValueError):
                    continue
        return cls(days)

    @property
    def covered_range(self) -> tuple[date, date] | None:
        return (min(self._days), max(self._days)) if self._days else None

    def is_trading_day(self, d: date) -> bool:
        """Observed where we have data; weekday heuristic outside that window."""
        if d in self._days:
            return True
        covered = self.covered_range
        if covered and covered[0] <= d <= covered[1]:
            return False          # inside the window and absent => holiday
        return d.weekday() < 5    # outside the window => best effort

    def is_market_open(self, moment: datetime) -> bool:
        local = moment.astimezone(IST)
        if not self.is_trading_day(local.date()):
            return False
        return MARKET_OPEN <= local.time() <= MARKET_CLOSE

    def trading_days_between(self, start: datetime, end: datetime) -> float:
        """Trading days elapsed, counting partial days as fractions.

        Fractional because a user who acknowledged at 10am and returned at 1pm
        has seen roughly half a session, not one day and not zero. Scoring a
        three-hour gap as a full day would understate every intraday surprise.
        """
        if end <= start:
            return 0.0

        start_l = start.astimezone(IST)
        end_l = end.astimezone(IST)
        session_seconds = (
            datetime.combine(date.today(), MARKET_CLOSE)
            - datetime.combine(date.today(), MARKET_OPEN)
        ).total_seconds()

        total = 0.0
        cursor = start_l.date()
        while cursor <= end_l.date():
            if self.is_trading_day(cursor):
                open_dt = datetime.combine(cursor, MARKET_OPEN, tzinfo=IST)
                close_dt = datetime.combine(cursor, MARKET_CLOSE, tzinfo=IST)
                overlap_start = max(start_l, open_dt)
                overlap_end = min(end_l, close_dt)
                if overlap_end > overlap_start:
                    total += (overlap_end - overlap_start).total_seconds() / session_seconds
            cursor += timedelta(days=1)
        return total


def resolve_freshness(
    fetched_at: datetime,
    now: datetime,
    calendar: TradingCalendar,
    fetch_failed: bool = False,
) -> Freshness:
    """Classify how much we should trust this datapoint right now.

    The key judgement: age only means "stale" while the market is OPEN. Once
    trading has closed, the last price of the session is not old data -- it is
    the correct answer, and it will stay the correct answer until the next
    open. Systems that apply a flat age threshold get this wrong every evening
    and every weekend.
    """
    if fetch_failed:
        return Freshness.UNAVAILABLE

    if not calendar.is_market_open(now):
        return Freshness.CLOSED

    age = (now - fetched_at).total_seconds()
    if age < 0:
        # Clock skew between our host and the provider. Treat as current
        # rather than crashing, but never as more current than LIVE.
        return Freshness.LIVE
    if age <= LIVE_SECONDS:
        return Freshness.LIVE
    if age <= DELAYED_SECONDS:
        return Freshness.DELAYED
    return Freshness.STALE
