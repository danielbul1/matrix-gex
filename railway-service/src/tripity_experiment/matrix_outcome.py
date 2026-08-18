"""Day-outcome classification and the label->outcome prediction mapping.

These rules decide whether a regime call (from the engine or from a user's
journal entry) was right, given what the session actually did. They are used
by BOTH tools/backtest_regime.py (historical replay) and the journal grading
in public_company_host.py (live workflow), so they live here exactly once —
never restate them elsewhere.

Outcome classification (explicit named-constant rules, applied to a session's
OHLC — the full day for journal grading, post-decision-time for backtests):
- PIN: the close lands within OUTCOME_PIN_WALL_PCT of a stored wall
  (call_wall or put_wall from the decision snapshot). PIN beats trend.
- TREND_UP / TREND_DOWN: trend efficiency |close-open| / (high-low) is at
  least TREND_EFFICIENCY and the net drift |close-open|/open is at least
  TREND_MIN_DRIFT_PCT; the sign of the drift sets the direction.
- RANGE: everything else (chop that neither pinned nor trended).

Prediction mapping (what each regime label bets on; MIXED bets on nothing and
is excluded from every accuracy denominator):
- GRIND_UP  -> TREND_UP, or RANGE with non-negative drift (upward drift)
- TRAP_DOOR -> TREND_DOWN
- SQUEEZE   -> TREND_UP
- PIN       -> PIN or RANGE
- MIXED     -> no prediction (excluded)

Pure stdlib, importable without the host's third-party deps.
"""

# --- Outcome classification rules (named constants; tune here only) ---
# Close within 0.5% of a stored wall counts as a successful pin.
OUTCOME_PIN_WALL_PCT = 0.005
# Trend: at least half the realized range must survive as net drift...
TREND_EFFICIENCY = 0.5
# ...and that drift must be at least 0.2% of the decision-time price.
TREND_MIN_DRIFT_PCT = 0.002

OUTCOMES = ("TREND_UP", "TREND_DOWN", "RANGE", "PIN")


def ohlc_from_bars(bars):
    """(open, high, low, close) over intraday bars, or None if empty."""
    if not bars:
        return None
    return (bars[0]["open"], max(b["high"] for b in bars),
            min(b["low"] for b in bars), bars[-1]["close"])


def ohlc_from_spots(points):
    """Synthetic (open, high, low, close) from a spot series, or None."""
    spots = [float(p["spot"]) for p in points if p.get("spot")]
    if not spots:
        return None
    return (spots[0], max(spots), min(spots), spots[-1])


def classify_outcome(open_, high, low, close, walls=()):
    """Classify a session as TREND_UP/TREND_DOWN/RANGE/PIN.

    Returns (outcome, drift, wall_hit) where drift = (close-open)/open.
    Rules (see module docstring): PIN beats trend — a close glued to a stored
    wall is a pin regardless of how it travelled there.
    """
    open_, high, low, close = (float(v) for v in (open_, high, low, close))
    if open_ <= 0:
        raise ValueError("open must be positive")
    drift = (close - open_) / open_
    for wall in walls or ():
        try:
            wall = float(wall)
        except (TypeError, ValueError):
            continue
        if wall > 0 and abs(close - wall) / close <= OUTCOME_PIN_WALL_PCT:
            return "PIN", drift, wall
    span = high - low
    efficiency = abs(close - open_) / span if span > 0 else 0.0
    if efficiency >= TREND_EFFICIENCY and abs(drift) >= TREND_MIN_DRIFT_PCT:
        return ("TREND_UP" if drift > 0 else "TREND_DOWN"), drift, None
    return "RANGE", drift, None


def predicts(label, outcome, drift):
    """What each engine label bets on. True/False = scored; None = excluded."""
    if label == "GRIND_UP":
        return outcome == "TREND_UP" or (outcome == "RANGE" and drift >= 0)
    if label == "TRAP_DOOR":
        return outcome == "TREND_DOWN"
    if label == "SQUEEZE":
        return outcome == "TREND_UP"
    if label == "PIN":
        return outcome in ("PIN", "RANGE")
    return None  # MIXED (or an unknown label): no prediction
