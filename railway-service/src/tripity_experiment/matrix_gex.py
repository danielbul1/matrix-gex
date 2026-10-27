"""Canonical Black-Scholes greeks + gamma-exposure (GEX) engine.

Single source of truth for the options math used by the Matrix GEX dashboard.
Pure stdlib (math.erf for the normal CDF) so it runs unchanged on Railway,
in GitHub Actions, and locally.

Conventions (SqueezeMetrics-style, matching web/matrix.js):
- Rates: a row's implied carry b = r - q ("b", from put-call parity on its
  expiry, #72) is the Black-Scholes rate with q = 0; rows without one use
  r = 0.05, q = 0.
- GEX = gamma * OI * mult * spot^2 * 0.01   (calls +, puts -)
- DEX = delta * OI * mult * spot  * 0.01    (put delta is negative, added as-is)
- VEX = vanna * OI * mult * spot  * 0.01    (calls +, puts -)
- CHEX = charm * OI * mult * spot / 252     (calls +, puts -; per trading day)
  Put charm equals call charm by put-call parity (delta_put = delta_call - 1),
  so the exposure-level minus sign on puts is a single negation.
- x0.01 factors express exposure per 1% move in spot / 1 vol-point move in IV.
- years_to_expiry is US/Eastern aware: AM-settled roots (SPX/NDX/VIX/VIXW)
  expire 9:30 ET, everything else 16:00 ET (13:00 ET on an NYSE early
  close), with a 1-minute floor.
- Gamma Flip uses the smooth method: BS-gamma profile over a spot ladder,
  the sign change nearest spot (not a cumulative-sum over strikes, and not
  the first crossing from the bottom of the ladder).

Option rows are mappings with keys: t ("C"/"P"), k (strike), oi, iv, g
(vendor gamma), d (vendor delta), exp ("YYYY-MM-DD"), root, and optionally
b (implied carry).
"""

import math
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
AM_SETTLED_ROOTS = {"SPX", "NDX", "VIX", "VIXW"}

DEFAULT_RATE = 0.05
DEFAULT_IV = 0.2
MIN_T = 1 / (365.25 * 24 * 60)  # 1-minute floor, preserves 0DTE decay
MIN_SIGMA = 0.01
TRADING_DAYS = 252  # charm is quoted per trading day

_MS_PER_YEAR = 365.25 * 86400000


# ---------- Normal distribution ----------
def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


# ---------- Black-Scholes greeks ----------
def d1d2(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> tuple[float, float]:
    T = max(T, MIN_T)
    sigma = max(sigma, MIN_SIGMA)
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq)
    return d1, d1 - sigma * sq


def bs_price(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE, is_call: bool = True) -> float:
    d1, d2 = d1d2(S, K, T, sigma, r)
    if is_call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE, is_call: bool = True) -> float:
    d1, _ = d1d2(S, K, T, sigma, r)
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1


def bs_gamma(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> float:
    """Same for calls and puts."""
    d1, _ = d1d2(S, K, T, sigma, r)
    T = max(T, MIN_T)
    sigma = max(sigma, MIN_SIGMA)
    return norm_pdf(d1) / (S * sigma * math.sqrt(T))


def bs_vega(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> float:
    """Per 1% (one vol point) move in IV."""
    d1, _ = d1d2(S, K, T, sigma, r)
    return S * math.sqrt(max(T, MIN_T)) * norm_pdf(d1) / 100


MAX_SIGMA = 5.0  # norm_iv's validity ceiling
IV_TOLERANCE = 1e-9


def implied_vol(price: float, S: float, K: float, T: float, r: float = DEFAULT_RATE,
                is_call: bool = True) -> float | None:
    """The sigma at which bs_price matches price, or None when no sigma in
    [MIN_SIGMA, MAX_SIGMA] does (a price outside the no-arbitrage bounds,
    or one too close to intrinsic to resolve). Newton steps on vega, kept
    inside a shrinking bisection bracket."""
    lo, hi = MIN_SIGMA, MAX_SIGMA
    if not (bs_price(S, K, T, lo, r, is_call) <= price <= bs_price(S, K, T, hi, r, is_call)):
        return None
    sigma = 0.2
    for _ in range(100):
        diff = bs_price(S, K, T, sigma, r, is_call) - price
        if diff > 0:
            hi = sigma
        else:
            lo = sigma
        if abs(diff) < IV_TOLERANCE or hi - lo < IV_TOLERANCE:
            break
        vega = bs_vega(S, K, T, sigma, r) * 100
        step = sigma - diff / vega if vega > 0 else lo
        sigma = step if lo < step < hi else (lo + hi) / 2
    return sigma


def bs_theta(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE, is_call: bool = True) -> float:
    """Per calendar day (annual theta / 365)."""
    d1, d2 = d1d2(S, K, T, sigma, r)
    T = max(T, MIN_T)
    sigma = max(sigma, MIN_SIGMA)
    sq = math.sqrt(T)
    decay = -(S * norm_pdf(d1) * sigma) / (2 * sq)
    if is_call:
        theta = decay - r * K * math.exp(-r * T) * norm_cdf(d2)
    else:
        theta = decay + r * K * math.exp(-r * T) * norm_cdf(-d2)
    return theta / 365


def bs_vanna(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> float:
    """Vanna = dDelta/dVol = -norm.pdf(d1) * d2 / sigma. Same for calls/puts."""
    d1, d2 = d1d2(S, K, T, sigma, r)
    sigma = max(sigma, MIN_SIGMA)
    return -norm_pdf(d1) * d2 / sigma


def bs_charm(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> float:
    """Charm = dDelta/dt. Identical for calls and puts by put-call parity;
    the put-side minus sign is applied at the exposure level (see CHEX)."""
    d1, d2 = d1d2(S, K, T, sigma, r)
    T = max(T, MIN_T)
    sigma = max(sigma, MIN_SIGMA)
    sq = math.sqrt(T)
    return -norm_pdf(d1) * (2 * r * T - d2 * sigma * sq) / (2 * T * sigma * sq)


# ---------- Time / IV helpers ----------
def is_early_close(day) -> bool:
    """NYSE early close (13:00 ET): the day after Thanksgiving, and July 3 and
    December 24 when they fall Monday to Thursday (on a Friday they are the
    observed holiday instead)."""
    if day.month == 11 and day.weekday() == 4:
        return 22 <= day.day - 1 <= 28  # the day before is the fourth Thursday
    if (day.month, day.day) in ((7, 3), (12, 24)):
        return day.weekday() <= 3
    return False


def years_to_expiry(exp: str, root: str, valuation_ms: float | None = None) -> float:
    """Years until expiry; AM-settled index roots expire 9:30 ET, else 16:00 ET,
    or 13:00 ET on an early-close day (#71).

    valuation_ms is epoch milliseconds (defaults to now). Result is floored
    at one minute.
    """
    if valuation_ms is None:
        valuation_ms = datetime.now(ET).timestamp() * 1000
    try:
        year, month, day = (int(part) for part in str(exp)[:10].split("-"))
    except ValueError:
        return MIN_T
    if root in AM_SETTLED_ROOTS:
        hour, minute = 9, 30
    elif is_early_close(datetime(year, month, day).date()):
        hour, minute = 13, 0
    else:
        hour, minute = 16, 0
    expiry = datetime(year, month, day, hour, minute, tzinfo=ET)
    return max((expiry.timestamp() * 1000 - valuation_ms) / _MS_PER_YEAR, MIN_T)


def norm_iv(iv: float) -> float:
    """Normalize implied vol: values above 3 are treated as percent
    (e.g. 13.5 -> 0.135). Returns 0.0 outside the validity window (0, 5)."""
    if iv > 3:
        iv /= 100
    return iv if 0 < iv < 5 else 0.0


# ---------- Implied carry from put-call parity (#72) ----------
CARRY_WINDOW = 0.03    # strikes within +/-3% of spot
CARRY_MIN_PAIRS = 5    # strikes with a two-sided call and put quote
CARRY_MAX_ABS = 0.25   # a carry beyond +/-25% a year is treated as bad data


def implied_carry(pairs, spot: float, T: float) -> float | None:
    """Net carry b = r - q for one expiry, from put-call parity, or None.

    pairs: (strike, call_mid, put_mid, call_spread + put_spread). Parity gives
    C - P = D * (F - K), so a spread-weighted line of C - P against K has
    slope -D and puts the forward at F = intercept / D; b = ln(F / spot) / T.
    Only the forward is used: D on its own is noisy, and American-style ETF
    options break parity by their early-exercise premium. Gamma depends on
    the forward, so Black-Scholes takes b as its rate, with q = 0."""
    if spot <= 0 or T <= 0:
        return None
    points = [(K, c - p, max(width, 1e-6)) for K, c, p, width in pairs
              if abs(K / spot - 1) <= CARRY_WINDOW]
    if len(points) < CARRY_MIN_PAIRS:
        return None
    weights = [1 / width ** 2 for _, _, width in points]
    total = sum(weights)
    k_mean = sum(w * K for w, (K, _, _) in zip(weights, points)) / total
    y_mean = sum(w * y for w, (_, y, _) in zip(weights, points)) / total
    spread_k = sum(w * (K - k_mean) ** 2 for w, (K, _, _) in zip(weights, points))
    if spread_k <= 0:
        return None
    discount = -sum(w * (K - k_mean) * (y - y_mean)
                    for w, (K, y, _) in zip(weights, points)) / spread_k
    if discount <= 0:
        return None
    forward = (y_mean + discount * k_mean) / discount
    if forward <= 0:
        return None
    carry = math.log(forward / spot) / T
    return carry if abs(carry) <= CARRY_MAX_ABS else None


def expiry_carries(quotes, spot: float, valuation_ms: float | None = None) -> dict[tuple[str, str], float]:
    """{(root, expiry): implied carry} from raw quotes
    (root, expiry "YYYY-MM-DD", "C"/"P", strike, bid, ask)."""
    sides: dict[tuple[str, str], dict[float, dict[str, tuple[float, float]]]] = {}
    for root, exp, call_put, K, bid, ask in quotes:
        bid, ask = float(bid or 0), float(ask or 0)
        if bid > 0 and ask > bid:
            sides.setdefault((root, exp), {}).setdefault(float(K), {})[call_put] = (
                (bid + ask) / 2, ask - bid)
    carries = {}
    for (root, exp), strikes in sides.items():
        pairs = [(K, q["C"][0], q["P"][0], q["C"][1] + q["P"][1])
                 for K, q in strikes.items() if "C" in q and "P" in q]
        carry = implied_carry(pairs, spot, years_to_expiry(exp, root, valuation_ms))
        if carry is not None:
            carries[(root, exp)] = carry
    return carries


def row_rate(row: dict, r: float = DEFAULT_RATE) -> float:
    """The Black-Scholes rate for one option row: its implied carry ("b")
    when the row has one, else r."""
    try:
        b = float(row.get("b"))
    except (TypeError, ValueError):
        return r
    return b if math.isfinite(b) else r


# ---------- Per-row effective greeks (vendor with BS fallback) ----------
def effective_greeks(
    row: dict,
    spot: float,
    valuation_ms: float | None = None,
    r: float = DEFAULT_RATE,
) -> tuple[float, float]:
    """(gamma, delta) for one option row: Black-Scholes from the row's IV at
    this spot and time. Vendor values are used only when the row has no IV,
    and Black-Scholes at the default IV (0.2) when neither exists.

    Vendor greeks are computed at the vendor's own underlying price, not at
    `spot`: on the 2026-10-26 Cboe close they turned SPX's Naive GEX from
    -21 to +11 $bn (#68)."""
    K = float(row.get("k") or 0)
    T = years_to_expiry(str(row.get("exp") or ""), str(row.get("root") or ""), valuation_ms)
    iv = float(row.get("iv") or 0)
    is_call = row.get("t") == "C"
    r = row_rate(row, r)
    if iv > 0:
        return bs_gamma(spot, K, T, iv, r), bs_delta(spot, K, T, iv, r, is_call)
    gamma = float(row.get("g") or 0)
    if gamma <= 0:
        gamma = bs_gamma(spot, K, T, DEFAULT_IV, r)
    delta = float(row.get("d") or 0)
    if delta == 0:
        delta = bs_delta(spot, K, T, DEFAULT_IV, r, is_call)
    return gamma, delta


def effective_gamma(row: dict, spot: float, valuation_ms: float | None = None, r: float = DEFAULT_RATE) -> float:
    """Vendor gamma when > 0, else BS gamma from the row's IV (default 0.2)."""
    return effective_greeks(row, spot, valuation_ms, r)[0]


# ---------- Exposure contributions ----------
def gex_value(gamma: float, oi: float, mult: float, spot: float) -> float:
    return gamma * oi * mult * spot * spot * 0.01


def dex_value(delta: float, oi: float, mult: float, spot: float) -> float:
    return delta * oi * mult * spot * 0.01


def vex_value(vanna: float, oi: float, mult: float, spot: float) -> float:
    return vanna * oi * mult * spot * 0.01


def chex_value(charm: float, oi: float, mult: float, spot: float) -> float:
    return charm * oi * mult * spot / TRADING_DAYS


# ---------- Aggregation ----------
def aggregate_strikes(
    options,
    spot: float,
    mult: float = 100,
    valuation_ms: float | None = None,
    r: float = DEFAULT_RATE,
    min_oi: float = 10,
    include_vc: bool = False,
) -> list[dict]:
    """Per-strike exposure rows (puts carry their minus sign), sorted by strike.

    Set include_vc=True to also aggregate vanna/charm exposure (BS-computed
    from the row IV when iv > 0).
    """
    by_strike: dict[float, list[float]] = {}
    # K -> [callGex, putGex, callDex, putDex, callVex, putVex, callChex, putChex]
    for option in options:
        try:
            oi = float(option.get("oi") or 0)
            K = float(option.get("k") or 0)
        except (TypeError, ValueError):
            continue
        if oi < min_oi or K <= 0:
            continue
        is_call = option.get("t") == "C"
        gamma, delta = effective_greeks(option, spot, valuation_ms, r)
        gex = gex_value(gamma, oi, mult, spot)
        dex = dex_value(delta, oi, mult, spot)
        vex = chex = 0.0
        if include_vc:
            iv = float(option.get("iv") or 0)
            if iv > 0:
                T = years_to_expiry(str(option.get("exp") or ""), str(option.get("root") or ""), valuation_ms)
                rate = row_rate(option, r)
                vex = vex_value(bs_vanna(spot, K, T, iv, rate), oi, mult, spot)
                chex = chex_value(bs_charm(spot, K, T, iv, rate), oi, mult, spot)
        row = by_strike.setdefault(K, [0.0] * 8)
        if is_call:
            row[0] += gex
            row[2] += dex
            row[4] += vex
            row[6] += chex
        else:
            row[1] -= gex
            row[3] += dex
            row[5] -= vex
            row[7] -= chex
    return [
        {
            "strike": K,
            "call_gex": v[0], "put_gex": v[1], "net_gex": v[0] + v[1],
            "call_dex": v[2], "put_dex": v[3], "net_dex": v[2] + v[3],
            "call_vex": v[4], "put_vex": v[5], "net_vex": v[4] + v[5],
            "call_chex": v[6], "put_chex": v[7], "net_chex": v[6] + v[7],
        }
        for K, v in sorted(by_strike.items())
    ]


def walls(rows: list[dict]) -> tuple[float | None, float | None]:
    """(call wall, put wall): strikes with the largest call / put GEX."""
    if not rows:
        return None, None
    call_wall = max(rows, key=lambda row: row["call_gex"])["strike"]
    put_wall = min(rows, key=lambda row: row["put_gex"])["strike"]
    return call_wall, put_wall


# ---------- Gamma profile / Gamma Flip ----------
FLIP_LOWER, FLIP_UPPER, FLIP_POINTS = 0.9, 1.1, 201  # spot grid: +/-10% in 201 points
FLIP_NEGLIGIBLE = 1e-9  # profile values below this share of gross gamma count as zero
FLIP_TOLERANCE = 1e-7   # bisection stops within this share of spot


class GammaFlip(NamedTuple):
    """The Gamma Flip: the crossing nearest spot, and every crossing found."""
    level: float
    crossings: list[float]


def _profile_rows(options, min_oi: float, valuation_ms: float | None,
                  r: float = DEFAULT_RATE) -> list[tuple]:
    """(K, T, iv, oi, sign, rate) for each row that can be repriced (IV > 0)."""
    rows = []
    for option in options:
        try:
            oi = float(option.get("oi") or 0)
            K = float(option.get("k") or 0)
        except (TypeError, ValueError):
            continue
        if oi < min_oi or K <= 0:
            continue
        iv = float(option.get("iv") or 0)
        if iv <= 0:
            continue
        T = years_to_expiry(str(option.get("exp") or ""), str(option.get("root") or ""), valuation_ms)
        sign = 1.0 if option.get("t") == "C" else -1.0
        rows.append((K, T, iv, oi, sign, row_rate(option, r)))
    return rows


def _gex_at(rows, S: float, mult: float) -> float:
    return sum(sign * bs_gamma(S, K, T, iv, rate) * oi
               for K, T, iv, oi, sign, rate in rows) * mult * S * S * 0.01


def gamma_profile(
    options,
    levels,
    mult: float = 100,
    valuation_ms: float | None = None,
    r: float = DEFAULT_RATE,
    min_oi: float = 0,
) -> list[float]:
    """Net GEX ($, calls + puts -) at each spot level, using BS gamma from
    each row's IV (vendor gamma is spot-stale, so the profile is model-based).
    """
    rows = _profile_rows(options, min_oi, valuation_ms, r)
    return [_gex_at(rows, S, mult) for S in levels]


def _sign_brackets(levels, profile, threshold: float = 0.0) -> list[tuple[int, int]]:
    """(i, k) index pairs where the profile changes sign between two
    neighbouring significant values (|value| > threshold)."""
    significant = [(j, v < 0) for j, v in enumerate(profile) if abs(v) > threshold]
    return [(i, k) for (i, i_neg), (k, k_neg) in zip(significant, significant[1:]) if i_neg != k_neg]


def flip_from_profile(levels, profile, spot: float, threshold: float = 0.0) -> float | None:
    """The sign change of a gamma profile nearest spot, linearly interpolated.
    None when the profile never changes sign. Values within threshold of zero
    count as zero, and a run of them between two signs puts the flip at its middle."""
    best = None
    for i, k in _sign_brackets(levels, profile, threshold):
        if k == i + 1:
            g0, g1 = profile[i], profile[k]
            x = levels[i] - (levels[k] - levels[i]) * g0 / (g1 - g0)
        else:
            x = (levels[i + 1] + levels[k - 1]) / 2
        if best is None or abs(x - spot) < abs(best - spot):
            best = float(x)
    return best


def gamma_flip(
    options,
    spot: float,
    mult: float = 100,
    valuation_ms: float | None = None,
    r: float = DEFAULT_RATE,
    min_oi: float = 0,
    lower: float = FLIP_LOWER,
    upper: float = FLIP_UPPER,
    points: int = FLIP_POINTS,
) -> GammaFlip | None:
    """The Gamma Flip: where Naive GEX, recomputed for the whole chain as if
    spot stood there, changes sign; the crossing nearest spot. None when it
    does not change sign within [lower, upper] x spot.

    Sweeps a spot grid, ignores values below FLIP_NEGLIGIBLE of gross gamma
    (so far-tail underflow cannot fake a crossing) and refines each crossing
    by bisection. The method follows mervinj3546/gexkit (research: open-source
    GEX survey, #69); no code is copied."""
    rows = _profile_rows(options, min_oi, valuation_ms, r)
    if not rows:
        return None
    gross = sum(oi * bs_gamma(K, K, T, iv, rate) * K * K
                for K, T, iv, oi, _, rate in rows) * mult * 0.01
    threshold = FLIP_NEGLIGIBLE * gross
    levels = [spot * (lower + (upper - lower) * j / (points - 1)) for j in range(points)]
    profile = [_gex_at(rows, S, mult) for S in levels]
    tolerance = FLIP_TOLERANCE * spot
    crossings = []
    for i, k in _sign_brackets(levels, profile, threshold):
        if k != i + 1:
            crossings.append((levels[i + 1] + levels[k - 1]) / 2)
            continue
        lo, hi, lo_negative = levels[i], levels[k], profile[i] < 0
        while hi - lo > tolerance:
            mid = (lo + hi) / 2
            value = _gex_at(rows, mid, mult)
            if abs(value) <= threshold:
                lo = hi = mid
            elif (value < 0) == lo_negative:
                lo = mid
            else:
                hi = mid
        crossings.append((lo + hi) / 2)
    if not crossings:
        return None
    level = min(crossings, key=lambda x: (abs(x - spot), x))
    return GammaFlip(float(level), [float(x) for x in crossings])


# ---------- Expected move ----------
def expected_move(
    options,
    spot: float,
    valuation_ms: float | None = None,
    r: float = DEFAULT_RATE,
) -> float | None:
    """Expected move from the ATM straddle at the nearest expiry."""
    best_T = None
    atm: dict[float, list[float]] = {}
    for option in options:
        iv = float(option.get("iv") or 0)
        if iv <= 0:
            continue
        T = years_to_expiry(str(option.get("exp") or ""), str(option.get("root") or ""), valuation_ms)
        if best_T is None or T < best_T:
            best_T = T
    if best_T is None:
        return None
    for option in options:
        iv = float(option.get("iv") or 0)
        if iv <= 0:
            continue
        T = years_to_expiry(str(option.get("exp") or ""), str(option.get("root") or ""), valuation_ms)
        if abs(T - best_T) > 1e-12:
            continue
        K = float(option.get("k") or 0)
        if K > 0:
            atm.setdefault(K, []).append(iv)
    if not atm:
        return None
    K = min(atm, key=lambda strike: abs(strike - spot))
    iv = sum(atm[K]) / len(atm[K])
    return bs_price(spot, K, best_T, iv, r, True) + bs_price(spot, K, best_T, iv, r, False)
