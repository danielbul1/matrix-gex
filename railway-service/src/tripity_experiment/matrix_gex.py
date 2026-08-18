"""Canonical Black-Scholes greeks + gamma-exposure (GEX) engine.

Single source of truth for the options math used by the Matrix GEX dashboard.
Pure stdlib (math.erf for the normal CDF) so it runs unchanged on Railway,
in GitHub Actions, and locally.

Conventions (SqueezeMetrics-style, matching web/matrix.js):
- Rates: r = 0.05, q = 0. (server.py historically used r = q = 0 and is being
  migrated to this canonical module.)
- GEX = gamma * OI * mult * spot^2 * 0.01   (calls +, puts -)
- DEX = delta * OI * mult * spot  * 0.01    (put delta is negative, added as-is)
- VEX = vanna * OI * mult * spot  * 0.01    (calls +, puts -)
- CHEX = charm * OI * mult * spot / 252     (calls +, puts -; per trading day)
  Put charm equals call charm by put-call parity (delta_put = delta_call - 1),
  so the exposure-level minus sign on puts is a single negation.
- x0.01 factors express exposure per 1% move in spot / 1 vol-point move in IV.
- years_to_expiry is US/Eastern aware: AM-settled roots (SPX/NDX/VIX/VIXW)
  expire 9:30 ET, everything else 16:00 ET, with a 1-minute floor.
- Zero-gamma flip uses the smooth method: BS-gamma profile over a spot ladder
  with sign-change interpolation (not a cumulative-sum over strikes).

Option rows are mappings with keys: t ("C"/"P"), k (strike), oi, iv, g
(vendor gamma), d (vendor delta), exp ("YYYY-MM-DD"), root.
"""

import math
from datetime import datetime
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
def years_to_expiry(exp: str, root: str, valuation_ms: float | None = None) -> float:
    """Years until expiry; AM-settled index roots expire 9:30 ET, else 16:00 ET.

    valuation_ms is epoch milliseconds (defaults to now). Result is floored
    at one minute.
    """
    if valuation_ms is None:
        valuation_ms = datetime.now(ET).timestamp() * 1000
    try:
        year, month, day = (int(part) for part in str(exp)[:10].split("-"))
    except ValueError:
        return MIN_T
    hour, minute = (9, 30) if root in AM_SETTLED_ROOTS else (16, 0)
    expiry = datetime(year, month, day, hour, minute, tzinfo=ET)
    return max((expiry.timestamp() * 1000 - valuation_ms) / _MS_PER_YEAR, MIN_T)


def norm_iv(iv: float) -> float:
    """Normalize implied vol: values above 3 are treated as percent
    (e.g. 13.5 -> 0.135). Returns 0.0 outside the validity window (0, 5)."""
    if iv > 3:
        iv /= 100
    return iv if 0 < iv < 5 else 0.0


# ---------- Per-row effective greeks (vendor with BS fallback) ----------
def effective_greeks(
    row: dict,
    spot: float,
    valuation_ms: float | None = None,
    r: float = DEFAULT_RATE,
) -> tuple[float, float]:
    """(gamma, delta) for one option row: vendor values when present,
    Black-Scholes from the row's IV (default 0.2) otherwise."""
    K = float(row.get("k") or 0)
    T = years_to_expiry(str(row.get("exp") or ""), str(row.get("root") or ""), valuation_ms)
    iv = float(row.get("iv") or 0)
    sigma = iv if iv > 0 else DEFAULT_IV
    is_call = row.get("t") == "C"
    gamma = float(row.get("g") or 0)
    if gamma <= 0:
        gamma = bs_gamma(spot, K, T, sigma, r)
    delta = float(row.get("d") or 0)
    if delta == 0:
        delta = bs_delta(spot, K, T, sigma, r, is_call)
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
                vex = vex_value(bs_vanna(spot, K, T, iv, r), oi, mult, spot)
                chex = chex_value(bs_charm(spot, K, T, iv, r), oi, mult, spot)
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


# ---------- Gamma profile / zero-gamma flip ----------
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
        rows.append((K, T, iv, oi, sign))
    profile = []
    for S in levels:
        total = 0.0
        for K, T, iv, oi, sign in rows:
            total += sign * bs_gamma(S, K, T, iv, r) * oi * mult * S * S * 0.01
        profile.append(total)
    return profile


def zero_gamma_flip(levels, profile) -> float | None:
    """First sign change in the gamma profile, linearly interpolated."""
    for i in range(len(profile) - 1):
        g0, g1 = profile[i], profile[i + 1]
        if g0 == 0:
            return float(levels[i])
        if g0 * g1 < 0:
            s0, s1 = levels[i], levels[i + 1]
            return float(s0 - (s1 - s0) * g0 / (g1 - g0))
    return None


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
