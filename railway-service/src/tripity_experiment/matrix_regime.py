"""Matrix regime engine — a single deterministic verdict fusing dealer-flow forces.

Pure stdlib (math + datetime), importable without the host's third-party deps.
No ML, no hidden state: the same inputs always produce the same verdict, and
every output carries the reasoning behind it (one sentence per force plus a
verdict sentence) so a user can see exactly why the engine said what it said.

Forces (each scores +1 tailwind / 0 neutral / -1 headwind):
- GEX  — dealer gamma dampens (positive GEX) or amplifies (negative GEX) moves;
  spot vs the zero-gamma flip is reported as context.
- VEX  — vanna wind. Dealers are typically short vanna (VEX < 0), so falling IV
  forces dealer buying (vanna bid, tailwind) and rising IV forces selling
  (headwind). The IV direction proxy is the ATM term-structure slope from the
  current chain: backwardation (front richer) = front-end stress that resolves
  into falling IV; steep contango = depressed IV with room to rise.
- CEX  — charm wind, weighted by proximity to the monthly OpEx (3rd Friday);
  charm flow only matters close to expiration.

Context (reported, not counted in the agreement score):
- VRP — vol risk premium: current front ATM IV minus realized vol
  (Garman-Klass / Parkinson over daily OHLC from the candles feed). IV rich
  favors premium selling; IV cheap favors option buyers.

Labels: GRIND_UP, TRAP_DOOR, SQUEEZE, PIN, MIXED.
"""

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# --- Thresholds (named constants; tune here only) ---
# Fraction of the summed absolute per-side exposure treated as noise. 0.04
# mirrors the existing snapshot regime rule in public_company_host.py.
GEX_NEUTRAL_BAND = 0.04
VEX_NEUTRAL_BAND = 0.04
CHEX_NEUTRAL_BAND = 0.04
# Term structure: |front ATM IV - back ATM IV| inside half a vol point is flat.
TERM_FLAT_BAND = 0.005
# Backwardation of 3+ vol points = front-end stress collapsing hard (SQUEEZE).
IV_FALLING_HARD_SLOPE = 0.03
# Charm wind: full strength inside 5 days of monthly OpEx, ignored beyond 10.
OPEX_FULL_WEIGHT_DAYS = 5
OPEX_FAR_DAYS = 10
# PIN: monthly OpEx within 3 days and spot pinned within 1% of a wall.
PIN_OPEX_DAYS = 3
PIN_WALL_PCT = 0.01
# VRP (IV - realized vol, decimal): +/-2 vol points is the rich/cheap edge.
VRP_RICH = 0.02
VRP_CHEAP = -0.02
# Realized-vol estimators need at least this many daily bars.
MIN_CANDLES = 5
TRADING_DAYS = 252

LABELS = ("GRIND_UP", "TRAP_DOOR", "SQUEEZE", "PIN", "MIXED")


# ---------------------------------------------------------------------------
# IV term structure (stress gauge — no VIX futures needed)
# ---------------------------------------------------------------------------
def atm_iv_term_structure(options, spot, valuation_ms=None):
    """ATM IV per expiry from a parsed option chain, plus the slope verdict.

    options: rows with k/t/iv/exp (the project's compact chain shape).
    Returns {"expiries": {exp: iv}, "front_iv", "back_iv", "slope", "state"}
    where slope = front - back (positive = backwardation = stress). state is
    "backwardation" | "contango" | "flat" | "unavailable".
    """
    del valuation_ms  # IV-per-expiry needs no clock; kept for a stable API
    if not spot or spot <= 0:
        return {"expiries": {}, "front_iv": None, "back_iv": None,
                "slope": None, "state": "unavailable"}
    by_exp: dict[str, dict[float, list[float]]] = {}
    for row in options or []:
        try:
            iv = float(row.get("iv") or 0)
            K = float(row.get("k") or 0)
        except (TypeError, ValueError):
            continue
        exp = str(row.get("exp") or "")
        if iv <= 0 or K <= 0 or not exp:
            continue
        by_exp.setdefault(exp, {}).setdefault(K, []).append(iv)
    expiries: dict[str, float] = {}
    for exp in sorted(by_exp):
        strikes = by_exp[exp]
        K = min(strikes, key=lambda strike: abs(strike - spot))
        expiries[exp] = sum(strikes[K]) / len(strikes[K])
    if not expiries:
        return {"expiries": {}, "front_iv": None, "back_iv": None,
                "slope": None, "state": "unavailable"}
    ordered = [expiries[exp] for exp in sorted(expiries)]
    front_iv = ordered[0]
    back_iv = sum(ordered[1:]) / len(ordered[1:]) if len(ordered) > 1 else ordered[0]
    slope = front_iv - back_iv
    if slope > TERM_FLAT_BAND:
        state = "backwardation"
    elif slope < -TERM_FLAT_BAND:
        state = "contango"
    else:
        state = "flat"
    return {"expiries": expiries, "front_iv": front_iv, "back_iv": back_iv,
            "slope": slope, "state": state}


# ---------------------------------------------------------------------------
# Realized volatility (Parkinson / Garman-Klass over daily OHLC bars)
# ---------------------------------------------------------------------------
def parkinson_volatility(candles):
    """Annualized Parkinson (high-low) volatility, or None if not enough data.

    candles: [{"open","high","low","close", ...}, ...] daily bars.
    sigma_ann = sqrt(252 * mean(ln(H/L)^2) / (4 ln 2))
    """
    total = 0.0
    n = 0
    for bar in candles or []:
        try:
            high, low = float(bar["high"]), float(bar["low"])
        except (KeyError, TypeError, ValueError):
            continue
        if high <= 0 or low <= 0 or high < low:
            continue
        total += math.log(high / low) ** 2
        n += 1
    if n < MIN_CANDLES:
        return None
    return math.sqrt(TRADING_DAYS * total / (4 * math.log(2) * n))


def garman_klass_volatility(candles):
    """Annualized Garman-Klass (OHLC) volatility, or None if not enough data.

    sigma_ann = sqrt(252 * mean(0.5 ln(H/L)^2 - (2 ln 2 - 1) ln(C/O)^2))
    """
    total = 0.0
    n = 0
    for bar in candles or []:
        try:
            o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0 or h < l:
            continue
        total += 0.5 * math.log(h / l) ** 2 - (2 * math.log(2) - 1) * math.log(c / o) ** 2
        n += 1
    if n < MIN_CANDLES:
        return None
    return math.sqrt(TRADING_DAYS * max(total, 0.0) / n)


def daily_ohlc(candles):
    """Collapse intraday candles ([{"time","open","high","low","close"}]) into
    daily OHLC bars keyed by US/Eastern session date, sorted ascending."""
    days: dict[date, dict] = {}
    for bar in candles or []:
        try:
            ts = float(bar["time"])
            o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        day = datetime.fromtimestamp(ts, tz=ET).date()
        agg = days.get(day)
        if agg is None:
            days[day] = {"date": day.isoformat(), "open": o, "high": h, "low": l, "close": c}
        else:
            agg["high"] = max(agg["high"], h)
            agg["low"] = min(agg["low"], l)
            agg["close"] = c
    return [days[day] for day in sorted(days)]


# ---------------------------------------------------------------------------
# Monthly OpEx proximity (charm context)
# ---------------------------------------------------------------------------
def third_friday(year, month):
    """The monthly options expiration date (3rd Friday) for year/month."""
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def days_to_monthly_opex(expiries, today=None):
    """Calendar days from `today` to the nearest monthly (3rd-Friday) expiry.

    expiries: iterable of "YYYY-MM-DD" strings from the chain. If the chain
    lists no 3rd-Friday expiry today or later, falls back to the calendar
    3rd Friday. Returns None only when nothing is computable.
    """
    if today is None:
        today = datetime.now(ET).date()
    candidates = []
    for exp in expiries or []:
        try:
            day = date.fromisoformat(str(exp)[:10])
        except ValueError:
            continue
        if day >= today and day == third_friday(day.year, day.month):
            candidates.append(day)
    if not candidates:
        probe = third_friday(today.year, today.month)
        if probe < today:
            year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            probe = third_friday(year, month)
        candidates.append(probe)
    return (min(candidates) - today).days


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
def _fmt_num(value):
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "--"


def _fmt_pct(value):
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "--"


def _deadzone(total, scale, band):
    try:
        scale = abs(float(scale))
    except (TypeError, ValueError):
        scale = 0.0
    return scale * band if scale > 0 else 0.0


def compute_regime(inputs: dict) -> dict:
    """Fuse GEX/VEX/CHEX forces + vol context into one labelled verdict.

    inputs keys (all optional; the engine degrades gracefully):
      spot, total_gex, total_dex, total_vex, total_chex, flip, call_wall,
      put_wall, options (chain rows for term structure + OpEx), candles
      (daily OHLC bars for realized vol), valuation_ms (epoch ms clock),
      gex_scale / vex_scale / chex_scale (sum of absolute per-side exposures;
      used only to set each force's noise deadzone), atm_iv (override for the
      VRP leg when no chain is available), term_slope (front-minus-back ATM
      IV override that reactivates the VEX leg when no chain is available —
      used by history replay, where only the slope is persisted).

    Returns {"label", "agreement", "direction", "forces", "context",
    "reasoning"} where reasoning is one sentence per force plus the verdict.
    """
    inputs = inputs or {}
    spot = float(inputs.get("spot") or 0)
    total_gex = float(inputs.get("total_gex") or 0)
    total_vex = float(inputs.get("total_vex") or 0)
    total_chex = float(inputs.get("total_chex") or 0)
    flip = inputs.get("flip")
    call_wall = inputs.get("call_wall")
    put_wall = inputs.get("put_wall")
    options = inputs.get("options") or []
    candles = inputs.get("candles") or []
    valuation_ms = inputs.get("valuation_ms")
    if valuation_ms:
        today = datetime.fromtimestamp(float(valuation_ms) / 1000, tz=ET).date()
    else:
        today = datetime.now(ET).date()

    reasoning: list[str] = []

    # --- Context: term structure, realized vol, OpEx proximity ---
    term = atm_iv_term_structure(options, spot, valuation_ms)
    if term["slope"] is None:
        # Replay override: no chain, but a stored front-minus-back slope
        # reactivates the VEX leg. State is derived exactly as in
        # atm_iv_term_structure.
        try:
            slope_override = inputs.get("term_slope")
            slope_override = float(slope_override) if slope_override is not None else None
        except (TypeError, ValueError):
            slope_override = None
        if slope_override is not None:
            if slope_override > TERM_FLAT_BAND:
                state = "backwardation"
            elif slope_override < -TERM_FLAT_BAND:
                state = "contango"
            else:
                state = "flat"
            term = {"expiries": {}, "front_iv": None, "back_iv": None,
                    "slope": slope_override, "state": state}
    realized_gk = garman_klass_volatility(candles)
    realized_pk = parkinson_volatility(candles)
    realized = realized_gk if realized_gk is not None else realized_pk
    expiries = [str(row.get("exp")) for row in options if isinstance(row, dict) and row.get("exp")]
    opex_days = days_to_monthly_opex(expiries, today)

    # --- GEX force ---
    gex_dz = _deadzone(total_gex, inputs.get("gex_scale"), GEX_NEUTRAL_BAND)
    if total_gex > gex_dz:
        gex_sign = 1
        gex_note = f"Total GEX {_fmt_num(total_gex)} is positive — dealers dampen moves (mean-revert)."
    elif total_gex < -gex_dz:
        gex_sign = -1
        gex_note = f"Total GEX {_fmt_num(total_gex)} is negative — dealers amplify moves (trend risk)."
    else:
        gex_sign = 0
        gex_note = f"Total GEX {_fmt_num(total_gex)} is inside the noise band — no gamma edge."
    if flip and spot > 0:
        side = "above" if spot >= float(flip) else "below"
        gex_note += f" Spot {_fmt_num(spot)} is {side} the zero-gamma flip {_fmt_num(flip)}."
    reasoning.append(gex_note)
    gex_force = {"sign": gex_sign, "value": total_gex,
                 "mode": "dampening" if gex_sign > 0 else "amplifying" if gex_sign < 0 else "neutral",
                 "flip": flip, "reasoning": gex_note}

    # --- VEX wind (vanna) ---
    vex_dz = _deadzone(total_vex, inputs.get("vex_scale"), VEX_NEUTRAL_BAND)
    slope = term["slope"]
    if slope is None:
        iv_dir = "unavailable"
    elif slope > TERM_FLAT_BAND:
        iv_dir = "falling"   # backwardated front end = stress resolving into lower IV
    elif slope < -TERM_FLAT_BAND:
        iv_dir = "rising"    # steep contango = depressed IV with room to rise
    else:
        iv_dir = "stable"
    if abs(total_vex) <= vex_dz:
        vex_sign = 0
        vex_note = f"Total VEX {_fmt_num(total_vex)} is inside the noise band — no vanna wind."
    elif iv_dir == "stable" or iv_dir == "unavailable":
        vex_sign = 0
        if iv_dir == "stable":
            vex_note = (f"Term structure is flat ({term['state']}); vanna exposure "
                        f"{_fmt_num(total_vex)} has no IV trend to ride.")
        else:
            vex_note = "No option chain available — vanna wind undetermined."
    else:
        short_vanna = total_vex < 0
        if (short_vanna and iv_dir == "falling") or (not short_vanna and iv_dir == "rising"):
            vex_sign = 1
        else:
            vex_sign = -1
        side = "short" if short_vanna else "long"
        wind = "tailwind (vanna bid)" if vex_sign > 0 else "headwind (vanna supply)"
        vex_note = (f"Dealers are {side} vanna (VEX {_fmt_num(total_vex)}) and the term structure "
                    f"is {term['state']} (slope {_fmt_pct(slope)}), i.e. IV {iv_dir} — {wind}.")
    reasoning.append(vex_note)
    vex_force = {"sign": vex_sign, "value": total_vex, "term_structure": term["state"],
                 "slope": slope, "iv_direction": iv_dir, "reasoning": vex_note}

    # --- CEX wind (charm), weighted by OpEx proximity ---
    chex_dz = _deadzone(total_chex, inputs.get("chex_scale"), CHEX_NEUTRAL_BAND)
    if opex_days is None:
        chex_weight = 0.0
    elif opex_days <= OPEX_FULL_WEIGHT_DAYS:
        chex_weight = 1.0
    elif opex_days >= OPEX_FAR_DAYS:
        chex_weight = 0.0
    else:
        chex_weight = (OPEX_FAR_DAYS - opex_days) / (OPEX_FAR_DAYS - OPEX_FULL_WEIGHT_DAYS)
    if chex_weight < 0.5:
        chex_sign = 0
        where = f"{opex_days} days" if opex_days is not None else "an unknown distance"
        chex_note = f"Monthly OpEx is {where} out — charm flow is too weak to count."
    elif abs(total_chex) <= chex_dz:
        chex_sign = 0
        chex_note = f"Total CHEX {_fmt_num(total_chex)} is inside the noise band near OpEx."
    else:
        chex_sign = 1 if total_chex > 0 else -1
        wind = "charm bid" if chex_sign > 0 else "charm drag"
        chex_note = (f"Total CHEX {_fmt_num(total_chex)} with monthly OpEx {opex_days} days away "
                     f"(weight {chex_weight:.2f}) — {wind} as dealer deltas decay.")
    reasoning.append(chex_note)
    chex_force = {"sign": chex_sign, "value": total_chex, "days_to_opex": opex_days,
                  "weight": round(chex_weight, 3), "reasoning": chex_note}

    # --- VRP context (not counted in agreement) ---
    atm_iv = inputs.get("atm_iv")
    if atm_iv is None:
        atm_iv = term["front_iv"]
    try:
        atm_iv = float(atm_iv) if atm_iv is not None else None
    except (TypeError, ValueError):
        atm_iv = None
    if atm_iv and realized is not None:
        vrp = atm_iv - realized
        if vrp >= VRP_RICH:
            vrp_state = "rich"
            vrp_take = "IV rich — favors premium selling, suppresses buyer expectancy."
        elif vrp <= VRP_CHEAP:
            vrp_state = "cheap"
            vrp_take = "IV cheap — favors option buyers."
        else:
            vrp_state = "fair"
            vrp_take = "IV fairly priced vs realized — no vol edge either way."
        vrp_note = (f"ATM IV {_fmt_pct(atm_iv)} vs realized vol {_fmt_pct(realized)} "
                    f"(VRP {_fmt_pct(vrp)}): {vrp_take}")
    else:
        vrp = None
        vrp_state = "unavailable"
        vrp_note = "VRP unavailable — missing ATM IV or not enough candles for realized vol."
    reasoning.append(vrp_note)
    vrp_force = {"state": vrp_state, "atm_iv": atm_iv, "realized_vol": realized,
                 "vrp": vrp, "reasoning": vrp_note}

    # --- Agreement + label ---
    signs = [gex_sign, vex_sign, chex_sign]
    up = signs.count(1)
    down = signs.count(-1)
    if up == 0 and down == 0:
        agreement, direction = 0, 0
    elif up >= down:
        agreement, direction = up, 1
    else:
        agreement, direction = down, -1

    near_wall = False
    wall_hit = None
    if spot > 0:
        for wall in (call_wall, put_wall):
            try:
                if wall and abs(float(wall) - spot) / spot <= PIN_WALL_PCT:
                    near_wall, wall_hit = True, float(wall)
                    break
            except (TypeError, ValueError):
                continue
    quiet_vol = (vrp is not None and vrp > 0) or (vrp is None and term["state"] != "backwardation")

    if (opex_days is not None and opex_days <= PIN_OPEX_DAYS and gex_sign == 1
            and near_wall and quiet_vol):
        label = "PIN"
        verdict = (f"Verdict PIN: monthly OpEx in {opex_days} day(s), positive gamma and spot "
                   f"glued to the {_fmt_num(wall_hit)} wall with quiet vol — expect gravitational chop.")
    elif slope is not None and slope >= IV_FALLING_HARD_SLOPE and vex_sign == 1 and gex_sign <= 0:
        label = "SQUEEZE"
        verdict = (f"Verdict SQUEEZE: IV is collapsing out of backwardation ({_fmt_pct(slope)} slope) "
                   f"into a vanna bid while gamma is weak/negative — forced dealer buying can squeeze price.")
    elif gex_sign == 1 and vex_sign == 1 and chex_sign == 1:
        label = "GRIND_UP"
        verdict = ("Verdict GRIND_UP: positive gamma dampens dips while falling IV (vanna bid) and "
                   "charm decay pull price higher — all 3 forces aligned.")
    elif gex_sign == -1 and vex_sign == -1:
        label = "TRAP_DOOR"
        verdict = ("Verdict TRAP_DOOR: negative gamma amplifies moves and rising IV forces vanna "
                   "selling — rallies get sold and breaks accelerate.")
    else:
        label = "MIXED"
        verdict = (f"Verdict MIXED: forces disagree ({agreement}/3 aligned) — no single wind dominates.")
    reasoning.append(verdict)

    return {
        "label": label,
        "agreement": agreement,
        "direction": direction,
        "forces": {"gex": gex_force, "vex": vex_force, "chex": chex_force, "vrp": vrp_force},
        "context": {
            "term_structure": term,
            "days_to_opex": opex_days,
            "atm_iv": atm_iv,
            "realized_vol": realized,
            "realized_vol_gk": realized_gk,
            "realized_vol_parkinson": realized_pk,
            "asof_date": today.isoformat(),
        },
        "reasoning": reasoning,
    }
