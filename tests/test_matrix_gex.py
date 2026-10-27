"""Tests for the canonical Matrix GEX engine (tripity_experiment.matrix_gex)."""
import math
import sys
from datetime import datetime
from pathlib import Path

import pytest
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
from tripity_experiment import matrix_gex as mg

ET = mg.ET


def ms(dt):
    return dt.timestamp() * 1000


# ---------- Black-Scholes golden values (S=100, K=100, T=1, sigma=0.2, r=0.05, q=0) ----------
S, K, T, SIG, R = 100.0, 100.0, 1.0, 0.2, 0.05


def test_bs_golden_values_crosschecked_with_scipy():
    import math
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (R + 0.5 * SIG**2) * T) / (SIG * sq)
    d2 = d1 - SIG * sq
    exp_call = S * norm.cdf(d1) - K * math.exp(-R * T) * norm.cdf(d2)
    exp_put = K * math.exp(-R * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    exp_gamma = norm.pdf(d1) / (S * SIG * sq)

    assert mg.bs_price(S, K, T, SIG, R, True) == pytest.approx(exp_call, rel=1e-12)
    assert mg.bs_price(S, K, T, SIG, R, False) == pytest.approx(exp_put, rel=1e-12)
    assert mg.bs_delta(S, K, T, SIG, R, True) == pytest.approx(norm.cdf(d1), rel=1e-12)
    assert mg.bs_delta(S, K, T, SIG, R, False) == pytest.approx(norm.cdf(d1) - 1, rel=1e-12)
    assert mg.bs_gamma(S, K, T, SIG, R) == pytest.approx(exp_gamma, rel=1e-12)
    assert mg.bs_vega(S, K, T, SIG, R) == pytest.approx(S * sq * norm.pdf(d1) / 100, rel=1e-12)
    assert mg.bs_vanna(S, K, T, SIG, R) == pytest.approx(-norm.pdf(d1) * d2 / SIG, rel=1e-12)
    exp_charm = -norm.pdf(d1) * (2 * R * T - d2 * SIG * sq) / (2 * T * SIG * sq)
    assert mg.bs_charm(S, K, T, SIG, R) == pytest.approx(exp_charm, rel=1e-12)

    # Textbook reference points (Hull), for readability of failures.
    assert exp_call == pytest.approx(10.4506, abs=1e-3)
    assert exp_put == pytest.approx(5.5735, abs=1e-3)
    assert exp_gamma == pytest.approx(0.018762, abs=1e-5)


def test_gamma_same_for_call_and_put_and_parity_holds():
    # bs_gamma has no is_call argument: it is identical for calls and puts.
    assert mg.bs_gamma(S, K, T, SIG, R) == mg.bs_gamma(S, K, T, SIG, R)
    call = mg.bs_price(S, K, T, SIG, R, True)
    put = mg.bs_price(S, K, T, SIG, R, False)
    import math
    assert call - put == pytest.approx(S - K * math.exp(-R * T), rel=1e-12)
    # Put delta = call delta - 1, hence charm/vanna identical across sides.
    assert mg.bs_delta(S, K, T, SIG, R, False) == pytest.approx(
        mg.bs_delta(S, K, T, SIG, R, True) - 1, rel=1e-12)


# ---------- Exposure sign conventions ----------
def _row(t, k, oi=1000, iv=0.2, g=0.0, d=0.0, exp="2026-08-21", root="SPY"):
    return {"t": t, "k": k, "oi": oi, "iv": iv, "g": g, "d": d, "exp": exp, "root": root}


VALUATION = ms(datetime(2026, 8, 17, 12, 0, tzinfo=ET))


def test_gex_sign_convention_calls_positive_puts_negative():
    rows = mg.aggregate_strikes([_row("C", 100), _row("P", 100)], S, 100, VALUATION)
    assert len(rows) == 1
    row = rows[0]
    assert row["call_gex"] > 0
    assert row["put_gex"] < 0
    assert row["net_gex"] == pytest.approx(row["call_gex"] + row["put_gex"])


def test_chex_is_per_trading_day_and_put_single_negation():
    calls = mg.aggregate_strikes([_row("C", 100)], S, 100, VALUATION, include_vc=True)[0]
    puts = mg.aggregate_strikes([_row("P", 100)], S, 100, VALUATION, include_vc=True)[0]
    assert calls["call_chex"] == pytest.approx(-puts["put_chex"], rel=1e-12)
    # /252 convention: chex = charm * oi * mult * spot / 252
    charm = mg.bs_charm(S, 100, mg.years_to_expiry("2026-08-21", "SPY", VALUATION), 0.2)
    assert calls["call_chex"] == pytest.approx(charm * 1000 * 100 * S / 252, rel=1e-9)


# ---------- Gamma Flip (smooth method, #69) ----------
def test_flip_within_expected_interval():
    # Calls concentrated at 95, puts at 105: net gamma flips sign in between.
    chain = [_row("C", 95, oi=50000), _row("P", 105, oi=50000)]
    levels = [90 + i * 0.5 for i in range(41)]  # 90..110
    profile = mg.gamma_profile(chain, levels, 100, VALUATION)
    flip = mg.flip_from_profile(levels, profile, S)
    assert flip is not None
    assert 95 < flip < 105
    # Interpolation sanity: profile values straddle zero around the flip.
    below = [lv for lv, p in zip(levels, profile) if lv <= flip]
    above = [lv for lv, p in zip(levels, profile) if lv >= flip]
    assert profile[levels.index(below[-1])] * profile[levels.index(above[0])] < 0


def test_flip_from_profile_none_and_linear_root():
    levels = [90, 100, 110]
    assert mg.flip_from_profile(levels, [1.0, 2.0, 3.0], 100) is None
    # Sign change between 90 (profile +2) and 100 (profile -4): linear root.
    assert mg.flip_from_profile(levels, [2.0, -4.0, -1.0], 100) == pytest.approx(90 + 10 * 2 / 6)


def test_flip_from_profile_takes_the_crossing_nearest_spot_not_the_first():
    # Crossings near 95 and near 145: spot at 140 must get the upper one
    # (the old rule returned the first from the bottom).
    levels = [90, 100, 140, 150]
    profile = [1.0, -1.0, -1.0, 1.0]
    assert mg.flip_from_profile(levels, profile, 140) == pytest.approx(145)
    assert mg.flip_from_profile(levels, profile, 96) == pytest.approx(95)


def test_flip_from_profile_threshold_skips_negligible_values():
    # A tail value that is only underflow noise (-1e-12) must not create a crossing.
    levels = [80, 90, 100, 110]
    assert mg.flip_from_profile(levels, [1e-12, -1e-12, 5.0, 6.0], 100, threshold=1e-9) is None
    # A run of negligible values between two signs: the flip is the run's middle.
    assert mg.flip_from_profile(levels, [3.0, 0.0, 0.0, -3.0], 100, threshold=1e-9) == pytest.approx(95)


def _two_flip_chain():
    # Puts at 90, calls at 100, puts at 110: the profile is - / + / - around 100.
    return [_row("P", 90, oi=40000), _row("C", 100, oi=60000), _row("P", 110, oi=40000)]


def test_gamma_flip_reports_every_crossing_and_picks_the_nearest():
    chain = _two_flip_chain()
    flip = mg.gamma_flip(chain, 103.0, 100, VALUATION)
    assert flip is not None
    assert len(flip.crossings) == 2
    low, high = sorted(flip.crossings)
    assert 90 < low < 100 < high < 110
    assert flip.level == high  # spot 103 sits nearer the upper crossing
    assert mg.gamma_flip(chain, 97.0, 100, VALUATION).level == pytest.approx(low, abs=1e-4)


def test_gamma_flip_matches_a_dense_grid_oracle():
    # Oracle: a 4001-point profile over the same range, linear root nearest spot.
    chain = _two_flip_chain()
    spot = 103.0
    levels = [spot * (0.9 + 0.2 * j / 4000) for j in range(4001)]
    oracle = mg.flip_from_profile(levels, mg.gamma_profile(chain, levels, 100, VALUATION), spot)
    assert mg.gamma_flip(chain, spot, 100, VALUATION).level == pytest.approx(oracle, abs=1e-3)


def test_gamma_flip_none_without_a_sign_change_or_rows():
    assert mg.gamma_flip([_row("C", 100), _row("C", 105)], S, 100, VALUATION) is None
    assert mg.gamma_flip([_row("C", 100, iv=0.0)], S, 100, VALUATION) is None
    assert mg.gamma_flip([], S, 100, VALUATION) is None


# ---------- years_to_expiry ----------
def test_years_to_expiry_am_vs_pm_settlement():
    valuation = ms(datetime(2026, 8, 17, 12, 0, tzinfo=ET))  # Monday noon ET
    t_spx = mg.years_to_expiry("2026-08-18", "SPX", valuation)  # AM: 9:30 ET
    t_spy = mg.years_to_expiry("2026-08-18", "SPY", valuation)  # PM: 16:00 ET
    hour_years = 1 / (365.25 * 24)
    assert t_spx == pytest.approx(21.5 * hour_years, rel=1e-9)
    assert t_spy == pytest.approx(28.0 * hour_years, rel=1e-9)
    assert t_spy - t_spx == pytest.approx(6.5 * hour_years, rel=1e-9)


def test_years_to_expiry_one_minute_floor():
    valuation = ms(datetime(2026, 8, 18, 15, 59, 30, tzinfo=ET))
    assert mg.years_to_expiry("2026-08-18", "SPY", valuation) == mg.MIN_T
    # Already past expiry -> still the floor, never zero/negative.
    assert mg.years_to_expiry("2026-08-17", "SPY", valuation) == mg.MIN_T
    # Unparseable date -> floor.
    assert mg.years_to_expiry("bad", "SPY", valuation) == mg.MIN_T


# ---------- norm_iv ----------
@pytest.mark.parametrize("is_call", [True, False])
@pytest.mark.parametrize("strike, years, sigma", [
    (100.0, 1.0, 0.2), (80.0, 0.02, 0.35), (103.0, 7 / 365.25, 0.12),
    (100.0, 1 / (365.25 * 24), 0.5), (95.0, 0.25, 1.8)])
def test_implied_vol_inverts_bs_price(is_call, strike, years, sigma):
    price = mg.bs_price(S, strike, years, sigma, R, is_call)
    assert mg.implied_vol(price, S, strike, years, R, is_call) == pytest.approx(sigma, abs=1e-6)


def test_implied_vol_none_outside_no_arbitrage_bounds():
    discounted_k = K * math.exp(-R * T)
    assert mg.implied_vol(S - discounted_k - 0.01, S, K, T, R, True) is None  # < intrinsic
    assert mg.implied_vol(S + 0.01, S, K, T, R, True) is None  # > the underlying
    assert mg.implied_vol(discounted_k + 0.01, S, K, T, R, False) is None  # > PV(strike)
    assert mg.implied_vol(0.0, S, K, T, R, False) is None


def test_norm_iv():
    assert mg.norm_iv(13.5) == pytest.approx(0.135)
    assert mg.norm_iv(0.135) == pytest.approx(0.135)
    assert mg.norm_iv(0) == 0.0
    assert mg.norm_iv(-0.2) == 0.0
    assert mg.norm_iv(600) == 0.0  # 600% -> 6.0, outside validity window


# ---------- effective_gamma ----------
def test_effective_greeks_use_black_scholes_whenever_the_row_has_iv():
    # Vendor greeks computed at another underlying are ignored when IV exists (#68).
    T_row = mg.years_to_expiry("2026-08-21", "SPY", VALUATION)
    gamma, delta = mg.effective_greeks(_row("C", 100, iv=0.25, g=0.05, d=0.9), S, VALUATION)
    assert gamma == pytest.approx(mg.bs_gamma(S, 100, T_row, 0.25), rel=1e-12)
    assert delta == pytest.approx(mg.bs_delta(S, 100, T_row, 0.25, is_call=True), rel=1e-12)


def test_effective_greeks_vendor_only_without_iv_then_default_iv():
    assert mg.effective_greeks(_row("P", 100, iv=0.0, g=0.05, d=-0.4), S, VALUATION) == (0.05, -0.4)
    T_row = mg.years_to_expiry("2026-08-21", "SPY", VALUATION)
    gamma, delta = mg.effective_greeks(_row("P", 100, iv=0.0), S, VALUATION)
    assert gamma == pytest.approx(mg.bs_gamma(S, 100, T_row, mg.DEFAULT_IV), rel=1e-12)
    assert delta == pytest.approx(mg.bs_delta(S, 100, T_row, mg.DEFAULT_IV, is_call=False), rel=1e-12)


def test_naive_gex_ignores_vendor_gamma_from_another_spot():
    # Vendor gamma that peaks above spot (as Cboe's did on 2026-10-26) must not move
    # Naive GEX: the strike sum agrees with the repriced profile at spot.
    chain = [_row("C", 105, g=0.09), _row("P", 95, g=0.001), _row("C", 100, g=0.06), _row("P", 100, g=0.02)]
    rows = mg.aggregate_strikes(chain, S, 100, VALUATION, min_oi=0)
    total = sum(row["net_gex"] for row in rows)
    assert total == pytest.approx(mg.gamma_profile(chain, [S], 100, VALUATION)[0], rel=1e-12)


# ---------- walls / expected move ----------
def test_walls_and_expected_move():
    # Gamma pinned at 0.01 so the wall tracks OI, not BS shape: without IV the
    # vendor value is used.
    pinned = [_row("C", 110, oi=5000, iv=0.0, g=0.01), _row("C", 100, oi=1000, iv=0.0, g=0.01),
              _row("P", 90, oi=8000, iv=0.0, g=0.01), _row("P", 100, oi=1000, iv=0.0, g=0.01)]
    rows = mg.aggregate_strikes(pinned, S, 100, VALUATION)
    call_wall, put_wall = mg.walls(rows)
    assert call_wall == 110
    assert put_wall == 90
    chain = [dict(row, iv=0.2) for row in pinned]
    em = mg.expected_move(chain, S, VALUATION)
    T_row = mg.years_to_expiry("2026-08-21", "SPY", VALUATION)
    assert em == pytest.approx(
        mg.bs_price(S, 100, T_row, 0.2, mg.DEFAULT_RATE, True)
        + mg.bs_price(S, 100, T_row, 0.2, mg.DEFAULT_RATE, False), rel=1e-9)
    # ATM straddle is within 20% of the 0.8*S*sigma*sqrt(T) approximation.
    import math
    assert em == pytest.approx(0.8 * S * 0.2 * math.sqrt(T_row), rel=0.2)
