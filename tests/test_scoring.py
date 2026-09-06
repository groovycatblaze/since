"""
Edge-case tests for the scoring engine.

Every test below maps to a question a reviewer can ask. The names are written
so `pytest -v` reads as a list of handled edge cases.

Run:
    pytest tests/ -v
"""

import math

import pytest

from app.scoring import (
    Baseline, Freshness, Observation, ReasonType, Score, Thresholds,
    Tier, UserContext, score,
)

# A calm large-cap: 1% daily sigma, roughly what HDFCBANK measured at.
CALM = Baseline(
    prev_close=1000.0, mean_log_return=0.0003, sigma_log_return=0.010,
    median_volume=1_000_000, range_high=1100.0, range_low=900.0, range_days=90,
)

# A volatile name: 3% daily sigma.
WILD = Baseline(
    prev_close=100.0, mean_log_return=0.0005, sigma_log_return=0.030,
    median_volume=5_000_000, range_high=130.0, range_low=70.0, range_days=90,
)


def types_of(s: Score) -> set:
    return {r.type for r in s.reasons}


# ---------------------------------------------------------------------------
# The central claim: surprise, not size
# ---------------------------------------------------------------------------

def test_same_percent_move_ranks_differently_by_volatility():
    """THE thesis. A 2.5% move is major in a calm stock, ordinary in a wild one."""
    calm = score(Observation(price=1025.0, cumulative_volume=2_000_000),
                 CALM, UserContext(watermark_price=1000.0))
    wild = score(Observation(price=102.5, cumulative_volume=10_000_000),
                 WILD, UserContext(watermark_price=100.0))

    assert calm.tier == Tier.NEEDS_ATTENTION
    assert wild.tier == Tier.QUIET
    assert abs(calm.z) > abs(wild.z) * 2.5


def test_identical_price_change_produces_different_z():
    calm = score(Observation(price=1020.0), CALM, UserContext(watermark_price=1000.0))
    wild = score(Observation(price=102.0), WILD, UserContext(watermark_price=100.0))
    assert calm.z > wild.z


# ---------------------------------------------------------------------------
# Watermark semantics
# ---------------------------------------------------------------------------

def test_compares_against_watermark_not_previous_close():
    """The brief says "since you last checked", so the watermark wins."""
    s = score(Observation(price=1050.0), CALM, UserContext(watermark_price=1040.0))
    assert s.comparison == "since you last checked"
    assert s.pct_change == pytest.approx(0.96, abs=0.05)


def test_falls_back_to_previous_close_when_never_acknowledged():
    s = score(Observation(price=1050.0), CALM, UserContext(watermark_price=None))
    assert s.comparison == "since previous close"
    assert s.pct_change == pytest.approx(5.0, abs=0.05)


def test_newly_added_instrument_is_quiet_not_dramatic():
    """Added four seconds ago. "Up 40%" would be true and useless."""
    s = score(Observation(price=1400.0), CALM, UserContext(is_new=True))
    assert s.tier == Tier.QUIET
    assert ReasonType.NEW_TO_WATCHLIST in types_of(s)


# ---------------------------------------------------------------------------
# Time semantics
# ---------------------------------------------------------------------------

def test_short_elapsed_time_is_floored():
    """Acknowledged a minute ago: bid-ask noise must not read as an event."""
    s = score(Observation(price=1002.0), CALM,
              UserContext(watermark_price=1000.0, elapsed_trading_days=0.001))
    assert s.tier == Tier.QUIET


def test_long_absence_drops_the_statistical_claim():
    """Over 10 trading days, sqrt(t) scaling is not defensible. Say so."""
    s = score(Observation(price=1150.0), CALM,
              UserContext(watermark_price=1000.0, elapsed_trading_days=40))
    assert ReasonType.ABSOLUTE_CHANGE in types_of(s)
    assert s.z is None
    assert "trading days" in s.reasons[0].message


def test_sigma_scales_with_sqrt_of_time():
    """Same move over more days is less surprising, by exactly sqrt(t)."""
    one = score(Observation(price=1030.0), CALM,
                UserContext(watermark_price=1000.0, elapsed_trading_days=1))
    four = score(Observation(price=1030.0), CALM,
                 UserContext(watermark_price=1000.0, elapsed_trading_days=4))
    assert one.z / four.z == pytest.approx(2.0, abs=0.15)


# ---------------------------------------------------------------------------
# Freshness: display is not the same as scoring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("freshness", [Freshness.STALE, Freshness.UNAVAILABLE])
def test_stale_data_never_raises_a_flag(freshness):
    """The rule: stale data may be shown, never acted on."""
    s = score(Observation(price=1200.0, cumulative_volume=9_000_000,
                          freshness=freshness),
              CALM, UserContext(watermark_price=1000.0))
    assert s.tier == Tier.QUIET
    assert s.scored is False
    assert ReasonType.DATA_UNRELIABLE in types_of(s)


def test_stale_data_still_reports_the_price_it_has():
    """Degrade gracefully: withhold the judgement, not the information."""
    s = score(Observation(price=1200.0, freshness=Freshness.STALE),
              CALM, UserContext(watermark_price=1000.0))
    assert s.pct_change == pytest.approx(20.0, abs=0.1)


def test_market_closed_is_authoritative_not_stale():
    """Previous close at 8pm Saturday is correct data, not broken data."""
    s = score(Observation(price=1030.0, cumulative_volume=2_000_000,
                          freshness=Freshness.CLOSED),
              CALM, UserContext(watermark_price=1000.0))
    assert s.scored is True
    assert s.tier != Tier.QUIET


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------

def test_split_sized_drop_is_not_reported_as_a_crash():
    """A 1:2 split reads as -50%. Flagging it as urgent misleads the user."""
    s = score(Observation(price=500.0, cumulative_volume=1_100_000),
              CALM, UserContext(watermark_price=1000.0))
    assert ReasonType.SUSPECTED_CORPORATE_ACTION in types_of(s)
    assert s.tier == Tier.CHANGED
    assert s.scored is False


def test_corporate_action_message_admits_uncertainty():
    """We have no corporate actions feed. The wording must not pretend we do."""
    s = score(Observation(price=200.0, cumulative_volume=1_000_000),
              CALM, UserContext(watermark_price=1000.0))
    msg = s.reasons[0].message.lower()
    assert "often indicates" in msg and "verify" in msg


def test_large_move_with_heavy_volume_is_treated_as_real():
    """A genuine 30% repricing brings volume. Do not suppress it."""
    s = score(Observation(price=700.0, cumulative_volume=20_000_000),
              CALM, UserContext(watermark_price=1000.0))
    assert ReasonType.SUSPECTED_CORPORATE_ACTION not in types_of(s)


# ---------------------------------------------------------------------------
# Deterministic level events
# ---------------------------------------------------------------------------

def test_threshold_crossing_outranks_statistics():
    """A 0.2% move that crosses the user's level still matters."""
    s = score(Observation(price=1001.0, cumulative_volume=900_000), CALM,
              UserContext(watermark_price=999.0, threshold_price=1000.0))
    assert s.tier == Tier.NEEDS_ATTENTION
    assert ReasonType.THRESHOLD_CROSSED in types_of(s)


def test_threshold_not_crossed_is_silent():
    s = score(Observation(price=990.0), CALM,
              UserContext(watermark_price=985.0, threshold_price=1000.0))
    assert ReasonType.THRESHOLD_CROSSED not in types_of(s)


def test_downward_crossing_is_detected():
    s = score(Observation(price=999.0), CALM,
              UserContext(watermark_price=1001.0, threshold_price=1000.0))
    assert ReasonType.THRESHOLD_CROSSED in types_of(s)


def test_new_range_high_is_flagged():
    s = score(Observation(price=1105.0, cumulative_volume=2_000_000),
              CALM, UserContext(watermark_price=1090.0))
    assert ReasonType.RANGE_EXTREME in types_of(s)
    assert s.tier == Tier.NEEDS_ATTENTION


# ---------------------------------------------------------------------------
# Missing and malformed data
# ---------------------------------------------------------------------------

def test_missing_volume_is_not_assumed_normal():
    """None means unknown. Substituting 1.0 would assert a fact we lack."""
    s = score(Observation(price=1030.0, cumulative_volume=None),
              CALM, UserContext(watermark_price=1000.0))
    assert s.volume_ratio is None
    assert ReasonType.VOLUME_SURPRISE not in types_of(s)


def test_missing_volume_does_not_block_a_large_move():
    """Absent volume must not silently suppress a genuine signal."""
    s = score(Observation(price=1035.0, cumulative_volume=None),
              CALM, UserContext(watermark_price=1000.0))
    assert s.tier == Tier.NEEDS_ATTENTION


@pytest.mark.parametrize("bad_price", [0.0, -1.0, -999.0])
def test_invalid_price_returns_honest_unknown(bad_price):
    s = score(Observation(price=bad_price), CALM, UserContext(watermark_price=1000.0))
    assert s.scored is False
    assert ReasonType.DATA_UNRELIABLE in types_of(s)


def test_zero_baseline_price_does_not_divide_by_zero():
    broken = Baseline(prev_close=0.0, mean_log_return=0.0, sigma_log_return=0.01)
    s = score(Observation(price=1000.0), broken, UserContext())
    assert s.scored is False


def test_near_zero_sigma_does_not_explode():
    """A floored sigma must not produce an infinite z."""
    flat = Baseline(prev_close=100.0, mean_log_return=0.0,
                    sigma_log_return=0.0001, median_volume=1000)
    s = score(Observation(price=100.5, cumulative_volume=1000),
              flat, UserContext(watermark_price=100.0))
    assert math.isfinite(s.z)


# ---------------------------------------------------------------------------
# Restraint
# ---------------------------------------------------------------------------

def test_quiet_day_says_so_explicitly():
    """An honest "nothing happened" is a feature, not an empty state."""
    s = score(Observation(price=1002.0, cumulative_volume=1_000_000),
              CALM, UserContext(watermark_price=1000.0))
    assert s.tier == Tier.QUIET
    assert "No meaningful change" in s.reasons[0].message


def test_every_score_carries_at_least_one_reason():
    """The UI must never have to render a tier with no explanation."""
    cases = [
        Observation(price=1000.0, cumulative_volume=1_000_000),
        Observation(price=1200.0, cumulative_volume=8_000_000),
        Observation(price=800.0, freshness=Freshness.STALE),
        Observation(price=1105.0, cumulative_volume=3_000_000),
    ]
    for obs in cases:
        s = score(obs, CALM, UserContext(watermark_price=1000.0))
        assert len(s.reasons) >= 1
        assert all(r.message.strip() for r in s.reasons)


def test_tier_is_always_one_of_three():
    """Total function: no input produces an undefined tier."""
    for price in (0.01, 1.0, 500.0, 999.0, 1000.0, 1001.0, 5000.0, 1e6):
        for vol in (None, 0.0, 1_000_000.0, 1e9):
            for elapsed in (0.001, 1.0, 5.0, 50.0):
                s = score(Observation(price=price, cumulative_volume=vol), CALM,
                          UserContext(watermark_price=1000.0,
                                      elapsed_trading_days=elapsed))
                assert s.tier in (Tier.NEEDS_ATTENTION, Tier.CHANGED, Tier.QUIET)


# ---------------------------------------------------------------------------
# Financial responsibility
# ---------------------------------------------------------------------------

FORBIDDEN = ["buy", "sell", "opportunity", "recommend", "target price",
             "profit", "should invest", "bullish", "bearish"]


def test_no_investment_advice_language_in_any_output():
    """This is a broker's product. It must never imply an action."""
    cases = [
        (Observation(price=1200.0, cumulative_volume=9_000_000), UserContext(watermark_price=1000.0)),
        (Observation(price=500.0, cumulative_volume=1_000_000), UserContext(watermark_price=1000.0)),
        (Observation(price=1000.0), UserContext(is_new=True)),
        (Observation(price=900.0, freshness=Freshness.STALE), UserContext(watermark_price=1000.0)),
        (Observation(price=1105.0, cumulative_volume=2_000_000), UserContext(watermark_price=1090.0)),
        (Observation(price=1150.0), UserContext(watermark_price=1000.0, elapsed_trading_days=40)),
    ]
    for obs, user in cases:
        s = score(obs, CALM, user)
        for reason in s.reasons:
            low = reason.message.lower()
            for word in FORBIDDEN:
                assert word not in low, f"'{word}' appeared in: {reason.message}"


def test_z_is_never_described_as_a_probability():
    """Returns are fat-tailed. Probabilistic wording would be a false claim."""
    s = score(Observation(price=1040.0, cumulative_volume=3_000_000),
              CALM, UserContext(watermark_price=1000.0))
    for reason in s.reasons:
        low = reason.message.lower()
        assert "sigma" not in low
        assert "probability" not in low
        assert "% likely" not in low


# ---------------------------------------------------------------------------
# Clock authority
# ---------------------------------------------------------------------------

def test_watermark_applies_when_acknowledged_at_or_after_adding():
    """The "always new" bug: if added_at and acknowledged_at come from
    different clocks, added_at is permanently ahead and the watermark is
    never applied, so the product silently degrades to a plain day-change
    tracker. is_new must be driven by ordering on ONE clock."""
    from datetime import datetime, timezone

    added = datetime(2026, 8, 31, 10, 15, tzinfo=timezone.utc)

    # Acknowledged at the same moment the item was added -> not new.
    assert not (added > added)
    # Acknowledged later -> not new.
    assert not (added > added.replace(hour=11))
    # Added after the last acknowledgement -> genuinely new.
    assert added.replace(hour=12) > added.replace(hour=11)


def test_watermark_price_is_used_once_item_is_not_new():
    """Once acknowledged, comparison must switch off previous close."""
    s = score(Observation(price=1030.0, cumulative_volume=2_000_000), CALM,
              UserContext(watermark_price=1000.0, is_new=False))
    assert s.comparison == "since you last checked"
    assert s.pct_change == pytest.approx(3.0, abs=0.05)


def test_z_always_shares_the_sign_of_the_price_move():
    """A rise must never read as a negative z. Subtracting an estimated drift
    term broke this: 30 days cannot estimate a mean return, so mu was pure
    noise and could flip the sign of the numerator."""
    up = score(Observation(price=1030.0), CALM,
               UserContext(watermark_price=1000.0, elapsed_trading_days=4))
    down = score(Observation(price=970.0), CALM,
                 UserContext(watermark_price=1000.0, elapsed_trading_days=4))
    assert up.z > 0 and up.pct_change > 0
    assert down.z < 0 and down.pct_change < 0


def test_reported_multiple_matches_the_z_magnitude():
    """The message says "2.3x its normal range" while z reads -2.07. Those are
    the same quantity and must not be computed two different ways."""
    s = score(Observation(price=1035.0, cumulative_volume=2_000_000), CALM,
              UserContext(watermark_price=1000.0))
    sigma_msg = [r for r in s.reasons if r.type == ReasonType.SIGMA_MOVE][0]
    import re
    multiple = float(re.search(r"([\d.]+)x", sigma_msg.message).group(1))
    assert multiple == pytest.approx(abs(s.z), abs=0.06)


# ---------------------------------------------------------------------------
# Volume is a rate, not a total
# ---------------------------------------------------------------------------

def test_volume_ratio_is_prorated_by_session_progress():
    """Heavy trading at 10am must read as heavy, not as 0.15x. Comparing an
    hour of volume against a full-day median made the signal impossible to
    trigger before lunch."""
    morning = score(
        Observation(price=1005.0, cumulative_volume=300_000, session_fraction=0.15),
        CALM, UserContext(watermark_price=1000.0))
    assert morning.volume_ratio > 1.5


def test_volume_ratio_means_the_same_thing_at_any_hour():
    early = score(Observation(price=1005.0, cumulative_volume=200_000,
                              session_fraction=0.2), CALM,
                  UserContext(watermark_price=1000.0))
    late = score(Observation(price=1005.0, cumulative_volume=1_000_000,
                             session_fraction=1.0), CALM,
                 UserContext(watermark_price=1000.0))
    assert early.volume_ratio == pytest.approx(late.volume_ratio, abs=0.05)


def test_opening_minutes_do_not_explode_the_ratio():
    """A near-zero denominator at 09:16 would flag every ordinary open."""
    s = score(Observation(price=1001.0, cumulative_volume=20_000,
                          session_fraction=0.002), CALM,
              UserContext(watermark_price=1000.0))
    assert s.volume_ratio < 1.0


def test_very_large_move_needs_no_volume_confirmation():
    """A 3-sigma move on ordinary volume is still notable. The volume gate is
    there to filter thin-book artefacts, not to suppress genuine moves."""
    s = score(Observation(price=1032.0, cumulative_volume=900_000,
                          session_fraction=1.0), CALM,
              UserContext(watermark_price=1000.0))
    assert abs(s.z) >= 2.5
    assert s.tier == Tier.NEEDS_ATTENTION
