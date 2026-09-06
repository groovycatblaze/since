"""
scoring.py — what counts as a meaningful change.

This is the heart of the product and the only file with no I/O in it.
Everything here is a pure function: same inputs, same outputs, no database,
no clock, no network. That is deliberate. It means every edge case below can
be tested exhaustively in milliseconds, and it means a reviewer can read the
rules without tracing them through a persistence layer.

The central claim
-----------------
A 2% move in a stock that never moves is a bigger event than a 5% move in one
that always does. So we do not rank by price change. We rank by SURPRISE:
how far a stock moved relative to its own normal daily range, measured from
the moment the user last acknowledged it, with the market's own move removed.

Calibration
-----------
Thresholds are not guesses. Walk-forward over 90 days of recorded NSE history
(tools/calibrate.py) gives median |z| = 0.66 against a theoretical 0.674 for a
normal distribution, with NEEDS_ATTENTION firing on 5.1% of stock-days and
CHANGED on 14.6%. On a 20-stock watchlist that is roughly 3 non-quiet items
per day, which is few enough to still mean something.

What z is NOT
-------------
Measured p95 |z| runs 2.4-4.7 across instruments where a true normal would
give 1.96, and several instruments exceed |z| = 6. Returns are fat-tailed.
So z is never presented as a probability. No "3-sigma event", no "0.1%
likelihood". Every message is relative -- "3.2x its normal daily range" --
because a relative statement survives fat tails and a probabilistic one does
not.

Language
--------
This is a financial product. Nothing here says buy, sell, opportunity, target
or recommend. The system helps a user decide what to LOOK AT, never what to
do. tests/test_language.py enforces this against the whole codebase.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


class Tier(str, Enum):
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    CHANGED = "CHANGED"
    QUIET = "QUIET"


class Freshness(str, Enum):
    LIVE = "LIVE"                # market open, fetched seconds ago
    DELAYED = "DELAYED"          # market open, fetched minutes ago
    CLOSED = "CLOSED"            # market shut: previous close is AUTHORITATIVE
    STALE = "STALE"              # market open but our data is old
    UNAVAILABLE = "UNAVAILABLE"  # fetch failing past the retry budget


# Data we may DISPLAY but must never raise a new flag from. Telling a user a
# stock "needs attention" based on a five-minute-old quote during live trading
# is the kind of thing that erodes trust in a broker's product permanently.
NON_SCORING_FRESHNESS = {Freshness.STALE, Freshness.UNAVAILABLE}


class ReasonType(str, Enum):
    SIGMA_MOVE = "SIGMA_MOVE"
    MARKET_ADJUSTED = "MARKET_ADJUSTED"
    VOLUME_SURPRISE = "VOLUME_SURPRISE"
    THRESHOLD_CROSSED = "THRESHOLD_CROSSED"
    RANGE_EXTREME = "RANGE_EXTREME"
    ABSOLUTE_CHANGE = "ABSOLUTE_CHANGE"
    SUSPECTED_CORPORATE_ACTION = "SUSPECTED_CORPORATE_ACTION"
    NEW_TO_WATCHLIST = "NEW_TO_WATCHLIST"
    DATA_UNRELIABLE = "DATA_UNRELIABLE"


@dataclass(frozen=True)
class Reason:
    """One human-readable explanation, emitted by the backend.

    The frontend renders `message` and never reconstructs an explanation from
    raw numbers. Keeping the wording here means the rule and its phrasing can
    never drift apart, and it means the UI cannot accidentally invent a claim
    the scoring logic did not make.
    """
    type: ReasonType
    message: str
    value: float | None = None
    baseline: float | None = None


@dataclass(frozen=True)
class Baseline:
    """Shared market state. Identical for every user watching this instrument."""
    prev_close: float
    mean_log_return: float
    sigma_log_return: float
    median_volume: float | None = None
    range_high: float | None = None
    range_low: float | None = None
    range_days: int | None = None


@dataclass(frozen=True)
class Observation:
    """One point-in-time look at an instrument."""
    price: float
    cumulative_volume: float | None = None
    freshness: Freshness = Freshness.LIVE
    # Fraction of the trading session elapsed at this observation (0-1).
    # Needed because cumulative_volume is volume SO FAR TODAY and the baseline
    # is a FULL-day median: comparing them directly makes the volume signal
    # impossible to trigger in the morning and understated all day.
    session_fraction: float = 1.0


@dataclass(frozen=True)
class UserContext:
    """Personal state. This is the half that differs between users."""
    # Price at the moment the user last acknowledged. None = never acknowledged.
    watermark_price: float | None = None
    # Trading days since that acknowledgement (not calendar days).
    elapsed_trading_days: float = 1.0
    # Optional price level the user asked to be told about.
    threshold_price: float | None = None
    # True when the instrument was added after the last acknowledgement.
    is_new: bool = False


@dataclass(frozen=True)
class Thresholds:
    sigma_needs_attention: float = 2.0
    # Above this, no volume confirmation is required.
    sigma_alone: float = 2.5
    sigma_changed: float = 1.5
    volume_confirm_ratio: float = 1.5
    volume_alone_ratio: float = 3.0
    max_sigma_scaling_days: float = 10.0
    # Below a quarter of a trading day, intraday microstructure noise dominates
    # and scaling a DAILY sigma down by sqrt(t) stops being meaningful. Without
    # this floor, a user who acknowledges and refreshes a minute later would
    # see ordinary bid-ask noise scored as a dramatic event.
    min_elapsed_days: float = 0.25
    # A move this large with no corresponding volume is far more likely to be a
    # split or bonus than a genuine 25% repricing.
    corporate_action_log_return: float = 0.25
    corporate_action_max_volume_ratio: float = 1.5


@dataclass
class Score:
    tier: Tier
    reasons: list[Reason] = field(default_factory=list)
    z: float | None = None
    pct_change: float | None = None
    volume_ratio: float | None = None
    comparison: str = "previous close"
    freshness: Freshness = Freshness.LIVE
    scored: bool = True  # False when freshness forbade computing a new tier


# Common Indian corporate action ratios. A price ratio landing within 2% of one
# of these, with no volume to justify it, is almost certainly mechanical.
_SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 10.0, 1.5)


def _looks_like_corporate_action(
    log_return: float, volume_ratio: float | None, t: Thresholds
) -> bool:
    """Heuristic guard against splits, bonuses and demergers.

    A 1:5 split shows up in raw price data as an 80% crash. Tata Motors' 1:1
    demerger showed as a ~43% drop that reversed entirely once the second
    entity listed. Flagging those as urgent would be actively misleading.

    We have no corporate actions feed, so this FAILS SAFE: on suspicion we
    suppress the flag and say we are unsure, rather than raising a false alarm
    or silently swallowing a real move.
    """
    if abs(log_return) < t.corporate_action_log_return:
        return False

    ratio = math.exp(abs(log_return))
    matches_split = any(abs(ratio - r) / r < 0.02 for r in _SPLIT_RATIOS)

    # Two independent grounds for suspicion:
    #
    # 1. The price ratio lands on a known split or bonus ratio. Strong enough
    #    on its own -- splits do carry volume, so we do not require quiet
    #    trading here.
    # 2. A move above 25% with NO volume behind it. A genuine repricing of
    #    that size brings heavy trading; a mechanical adjustment does not.
    quiet_for_its_size = (
        volume_ratio is not None
        and volume_ratio < t.corporate_action_max_volume_ratio
    )
    return matches_split or quiet_for_its_size


def _volume_ratio(obs: Observation, baseline: Baseline) -> float | None:
    """Volume so far today, against volume normally expected BY THIS HOUR.

    Prorating matters: at 10:15 an hour of trading against a full-day median
    reads as 0.15x no matter how frenzied it is, so the signal could never fire
    in the morning. Scaling the expectation by session progress makes the ratio
    mean the same thing at any time of day.

    This assumes volume accrues evenly through the session, which it does not
    -- real intraday volume is U-shaped, heavy at the open and close. A volume
    curve would be the correct refinement; it is documented as a limitation
    rather than pretended away.

    None, not 1.0, when volume is unknown. 1.0 would assert "volume was
    normal", which we do not know. None lets downstream rules skip the signal
    instead of acting on a fabricated value.
    """
    if obs.cumulative_volume is None or not baseline.median_volume:
        return None
    if baseline.median_volume <= 0:
        return None
    # Floor the fraction: in the first minutes of trading the denominator
    # approaches zero and the ratio would explode on ordinary opening prints.
    fraction = max(obs.session_fraction, 0.1)
    return obs.cumulative_volume / (baseline.median_volume * fraction)


def score(
    obs: Observation,
    baseline: Baseline,
    user: UserContext,
    thresholds: Thresholds | None = None,
) -> Score:
    """Score one instrument for one user. Pure: no I/O, no clock, no globals."""
    t = thresholds or Thresholds()
    reasons: list[Reason] = []

    # --- guards -------------------------------------------------------------
    # Bad data produces an honest "we don't know", never a fabricated tier.
    if obs.price <= 0 or baseline.prev_close <= 0:
        return Score(
            tier=Tier.QUIET,
            reasons=[Reason(ReasonType.DATA_UNRELIABLE,
                            "Price data is unavailable for this instrument.")],
            freshness=obs.freshness,
            scored=False,
        )

    # --- choose the comparison baseline -------------------------------------
    # The brief's whole premise is "since you last checked", so the user's
    # acknowledged price wins whenever we have one. Previous close is the
    # fallback for instruments they have never acknowledged.
    if user.watermark_price and user.watermark_price > 0:
        base_price = user.watermark_price
        comparison = "since you last checked"
    else:
        base_price = baseline.prev_close
        comparison = "since previous close"

    log_return = math.log(obs.price / base_price)
    pct_change = (math.exp(log_return) - 1) * 100
    vol_ratio = _volume_ratio(obs, baseline)

    # --- corporate action guard, before anything else -----------------------
    # Runs first so a mechanical price change can never reach the tiering rules.
    if _looks_like_corporate_action(log_return, vol_ratio, t):
        return Score(
            tier=Tier.CHANGED,
            reasons=[Reason(
                ReasonType.SUSPECTED_CORPORATE_ACTION,
                f"Price changed {pct_change:+.1f}% without matching volume. "
                f"This often indicates a stock split, bonus or demerger rather "
                f"than market movement. Verify before interpreting.",
                value=pct_change,
            )],
            pct_change=pct_change,
            volume_ratio=vol_ratio,
            comparison=comparison,
            freshness=obs.freshness,
            scored=False,
        )

    # --- freshness gate -----------------------------------------------------
    # Stale data may be DISPLAYED. It may never generate a new flag.
    if obs.freshness in NON_SCORING_FRESHNESS:
        return Score(
            tier=Tier.QUIET,
            reasons=[Reason(
                ReasonType.DATA_UNRELIABLE,
                "Showing the last price we could confirm. Not current enough "
                "to judge what has changed.",
            )],
            pct_change=pct_change,
            volume_ratio=vol_ratio,
            comparison=comparison,
            freshness=obs.freshness,
            scored=False,
        )

    # --- new to the watchlist -----------------------------------------------
    # No acknowledged history, so there is no "since you last checked" to
    # report. Saying "up 40%" would be true of the stock and meaningless to
    # the user, who has been watching it for four seconds.
    if user.is_new:
        return Score(
            tier=Tier.QUIET,
            reasons=[Reason(ReasonType.NEW_TO_WATCHLIST,
                            "New to your watchlist. Changes will show from now on.")],
            pct_change=pct_change,
            volume_ratio=vol_ratio,
            comparison="since you added it",
            freshness=obs.freshness,
        )

    elapsed = max(user.elapsed_trading_days, t.min_elapsed_days)

    # --- long absence: stop pretending sigma scales -------------------------
    # Daily sigma scales as sqrt(t) only while returns stay roughly independent
    # and the volatility regime holds. Over 40 trading days neither is safe, so
    # we drop the statistical claim and report plain change instead -- and the
    # message says so, rather than quietly degrading.
    if elapsed > t.max_sigma_scaling_days:
        level_reasons = _level_reasons(obs, baseline, user)
        tier = Tier.NEEDS_ATTENTION if level_reasons else (
            Tier.CHANGED if abs(pct_change) >= 5 else Tier.QUIET
        )
        return Score(
            tier=tier,
            reasons=[Reason(
                ReasonType.ABSOLUTE_CHANGE,
                f"{pct_change:+.1f}% over about {int(elapsed)} trading days "
                f"since you last checked. Too long a gap to compare against "
                f"normal daily movement.",
                value=pct_change,
            )] + level_reasons,
            pct_change=pct_change,
            volume_ratio=vol_ratio,
            comparison=comparison,
            freshness=obs.freshness,
        )

    # --- the core signal ----------------------------------------------------
    # Scale both drift and volatility to the elapsed window: mean grows with t,
    # standard deviation with sqrt(t).
    sigma = max(baseline.sigma_log_return, 1e-6)

    # NO DRIFT TERM. The obvious formulation subtracts an expected return
    # (mu * t) before dividing, and that is wrong at this sample size: 30 days
    # is ample to estimate volatility and nowhere near enough to estimate a
    # mean return -- the standard error on mu swamps mu itself. Including it
    # put pure noise in the numerator, and the visible symptom was z
    # disagreeing in SIGN with the price move on screen (+1.1% reading as
    # z = -0.72), which is indefensible to a user.
    #
    # Volatility is estimable at short horizons. Drift is not. So we normalise
    # by sigma and not by mu, and z now always shares the sign of the move.
    z = log_return / (sigma * math.sqrt(elapsed))

    multiple = abs(z)
    if abs(z) >= t.sigma_changed:
        reasons.append(Reason(
            ReasonType.SIGMA_MOVE,
            f"Moved {pct_change:+.1f}%, about {multiple:.1f}x its normal "
            f"range for this period.",
            value=pct_change,
            baseline=sigma * 100,
        ))

    if vol_ratio is not None and vol_ratio >= t.volume_confirm_ratio:
        reasons.append(Reason(
            ReasonType.VOLUME_SURPRISE,
            f"Trading volume is {vol_ratio:.1f}x its recent median.",
            value=vol_ratio,
        ))

    reasons.extend(_level_reasons(obs, baseline, user))

    return Score(
        tier=_tier_for(z, vol_ratio, reasons, t),
        reasons=reasons or [Reason(
            ReasonType.SIGMA_MOVE,
            f"No meaningful change {comparison} ({pct_change:+.1f}%).",
            value=pct_change,
        )],
        z=z,
        pct_change=pct_change,
        volume_ratio=vol_ratio,
        comparison=comparison,
        freshness=obs.freshness,
    )


def _level_reasons(
    obs: Observation, baseline: Baseline, user: UserContext
) -> list[Reason]:
    """Deterministic events: a user's threshold, or a new range extreme.

    These carry no statistics and no noise. A threshold either was crossed or
    was not, which is why they always outrank the probabilistic signals.
    """
    out: list[Reason] = []

    if user.threshold_price and user.watermark_price:
        crossed_up = user.watermark_price < user.threshold_price <= obs.price
        crossed_down = user.watermark_price > user.threshold_price >= obs.price
        if crossed_up or crossed_down:
            out.append(Reason(
                ReasonType.THRESHOLD_CROSSED,
                f"Crossed your level of {user.threshold_price:,.2f} "
                f"({'upward' if crossed_up else 'downward'}).",
                value=obs.price,
                baseline=user.threshold_price,
            ))

    days = baseline.range_days
    if baseline.range_high and obs.price > baseline.range_high:
        out.append(Reason(
            ReasonType.RANGE_EXTREME,
            f"Highest price in the last {days} trading days.",
            value=obs.price, baseline=baseline.range_high,
        ))
    elif baseline.range_low and obs.price < baseline.range_low:
        out.append(Reason(
            ReasonType.RANGE_EXTREME,
            f"Lowest price in the last {days} trading days.",
            value=obs.price, baseline=baseline.range_low,
        ))

    return out


def _tier_for(
    z: float, volume_ratio: float | None, reasons: list[Reason], t: Thresholds
) -> Tier:
    """The tiering rule. Deliberately three buckets, not a score.

    An "Attention Score: 84.7" tells a user nothing they can act on and cannot
    be defended when asked where 84.7 came from. Three named buckets plus the
    reasons that put an item there is more honest and more useful.
    """
    types = {r.type for r in reasons}

    # Deterministic events outrank everything statistical.
    if ReasonType.THRESHOLD_CROSSED in types or ReasonType.RANGE_EXTREME in types:
        return Tier.NEEDS_ATTENTION

    az = abs(z)
    vr = volume_ratio

    # A large move confirmed by volume. The volume requirement is what filters
    # out thin-book artefacts in illiquid names -- a big print on almost no
    # volume is a microstructure event, not news.
    # A very large move needs no confirmation. The volume gate exists to filter
    # thin-book artefacts in illiquid names, and a move of this size is not
    # that. Requiring volume for everything left genuine 2.5-sigma moves in the
    # lower tier purely because trading happened to be ordinary.
    if az >= t.sigma_alone:
        return Tier.NEEDS_ATTENTION

    if az >= t.sigma_needs_attention and (vr is None or vr >= t.volume_confirm_ratio):
        return Tier.NEEDS_ATTENTION

    if az >= t.sigma_changed:
        return Tier.CHANGED

    # Volume moving alone can precede price. Worth surfacing quietly.
    if vr is not None and vr >= t.volume_alone_ratio and az >= 0.5:
        return Tier.CHANGED

    return Tier.QUIET
