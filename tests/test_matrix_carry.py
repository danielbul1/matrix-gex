"""Implied carry (#72): each expiry's b = r - q from put-call parity becomes
the Black-Scholes rate (with q = 0) in the engine, in both Cboe compactions
and in the dashboard."""
import datetime
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
import fetch_cboe
from tripity_experiment import matrix_gex as mg
from tripity_experiment import public_company_host as host
from test_matrix_greek_source import HARNESS, MATRIX_JS

ET = mg.ET
VALUATION = datetime.datetime(2026, 10, 27, 10, 0, tzinfo=ET)
VALUATION_MS = VALUATION.timestamp() * 1000
EXPIRY = "2026-11-20"
SPOT = 100.0
R, Q = 0.04, 0.015


def _parity_pairs(spot, T, r=R, q=Q, strikes=range(97, 104)):
    """Call and put mids that satisfy European put-call parity exactly."""
    pairs = []
    for K in strikes:
        put = 2.0 + abs(K - spot) * 0.1
        call = put + spot * math.exp(-q * T) - K * math.exp(-r * T)
        pairs.append((float(K), call, put, 0.1))
    return pairs


def test_implied_carry_recovers_r_minus_q_from_parity():
    T = mg.years_to_expiry(EXPIRY, "SPY", VALUATION_MS)
    carry = mg.implied_carry(_parity_pairs(SPOT, T), SPOT, T)
    # The forward is S e^{(r-q)T}, so b = r - q exactly.
    assert carry == pytest.approx(R - Q, abs=1e-9)


def test_implied_carry_none_without_enough_quotes_or_with_bad_data():
    T = mg.years_to_expiry(EXPIRY, "SPY", VALUATION_MS)
    assert mg.implied_carry(_parity_pairs(SPOT, T)[:4], SPOT, T) is None
    # Strikes outside the +/-3% window do not count.
    assert mg.implied_carry(_parity_pairs(SPOT, T, strikes=range(110, 120)), SPOT, T) is None
    # A carry beyond +/-25% a year is bad data.
    assert mg.implied_carry(_parity_pairs(SPOT, T, r=0.9, q=0.0), SPOT, T) is None
    assert mg.implied_carry(_parity_pairs(SPOT, T), SPOT, 0.0) is None


def _quotes(root="SPY", exp=EXPIRY, spot=SPOT):
    T = mg.years_to_expiry(exp, root, VALUATION_MS)
    quotes = []
    for K, call, put, _ in _parity_pairs(spot, T):
        quotes.append((root, exp, "C", K, call - 0.025, call + 0.025))
        quotes.append((root, exp, "P", K, put - 0.025, put + 0.025))
    return quotes


def test_expiry_carries_groups_by_root_and_expiry_and_skips_one_sided_quotes():
    quotes = _quotes() + _quotes(root="SPYW") + [("SPY", "2026-12-18", "C", 100.0, 0, 1.0)]
    carries = mg.expiry_carries(quotes, SPOT, VALUATION_MS)
    assert set(carries) == {("SPY", EXPIRY), ("SPYW", EXPIRY)}
    assert carries[("SPY", EXPIRY)] == pytest.approx(R - Q, abs=1e-9)


def test_row_rate_and_greeks_use_the_rows_carry():
    row = {"t": "C", "k": 102.0, "oi": 1000, "iv": 0.2, "exp": EXPIRY, "root": "SPY", "b": 0.02}
    assert mg.row_rate(row) == 0.02
    assert mg.row_rate({**row, "b": None}) == mg.DEFAULT_RATE
    assert mg.row_rate({**row, "b": float("nan")}, 0.03) == 0.03
    T = mg.years_to_expiry(EXPIRY, "SPY", VALUATION_MS)
    gamma, delta = mg.effective_greeks(row, SPOT, VALUATION_MS)
    assert gamma == pytest.approx(mg.bs_gamma(SPOT, 102.0, T, 0.2, 0.02), rel=1e-12)
    assert delta == pytest.approx(mg.bs_delta(SPOT, 102.0, T, 0.2, 0.02, True), rel=1e-12)
    # The repriced profile uses the same rate as the strike sum.
    (strike,) = mg.aggregate_strikes([row], SPOT, 100, VALUATION_MS, min_oi=0)
    assert strike["net_gex"] == pytest.approx(
        mg.gamma_profile([row], [SPOT], 100, VALUATION_MS)[0], rel=1e-12)


def _cboe_payload(spot=SPOT):
    options = []
    for root, exp, call_put, K, bid, ask in _quotes(spot=spot):
        symbol = f"{root}{exp[2:4]}{exp[5:7]}{exp[8:10]}{call_put}{int(K * 1000):08d}"
        options.append({"option": symbol, "bid": bid, "ask": ask, "iv": 0.2,
                        "open_interest": 500, "volume": 10, "gamma": 0.01, "delta": 0.5})
    return {"current_price": spot, "last_trade_time": VALUATION.strftime("%Y-%m-%dT%H:%M:%S"),
            "options": options}


def test_fetch_cboe_rows_carry_their_expirys_implied_carry(monkeypatch):
    monkeypatch.setattr(fetch_cboe, "fetch", lambda sym: _cboe_payload())
    record = fetch_cboe.fetch_chain("SPY", VALUATION.date())
    assert record["opts"]
    assert all(row["b"] == pytest.approx(R - Q, abs=1e-5) for row in record["opts"])


def test_railway_compaction_rows_carry_the_same_implied_carry():
    _, record = host._compact_matrix_options_payload(
        "SPY", {"data": _cboe_payload()}, today=VALUATION.date())
    assert record["opts"]
    assert all(row["b"] == pytest.approx(R - Q, abs=1e-5) for row in record["opts"])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run matrix.js")
def test_dashboard_uses_the_rows_carry():
    asof = VALUATION.isoformat()
    opts = [{"k": float(k), "t": t, "root": "SPY", "exp": EXPIRY, "dte": 24, "iv": 0.2,
             "oi": 1500 if t == "P" else 1000, "vol": 0, "g": 0, "d": 0, "b": 0.0}
            for k in (90, 95, 100, 105, 110) for t in ("C", "P")]
    record = {"symbol": "SPY", "spot": SPOT, "mult": 100, "asof": asof, "price_asof": asof,
              "source": "tripity_cboe", "opts": opts}
    run = subprocess.run(["node", "-e", HARNESS, str(MATRIX_JS), json.dumps({"carry": record})],
                         capture_output=True, text=True, timeout=60, check=True)
    total = json.loads(run.stdout.strip().splitlines()[-1])["carry"]
    expected = sum(r["net_gex"] for r in mg.aggregate_strikes(opts, SPOT, 100, VALUATION_MS))
    at_default = sum(r["net_gex"] for r in mg.aggregate_strikes(
        [{**o, "b": None} for o in opts], SPOT, 100, VALUATION_MS))
    assert abs(expected - at_default) > 1e-3 * abs(expected)  # the carry moves the answer
    assert total == pytest.approx(expected, rel=1e-6)


def test_an_expiry_under_a_day_borrows_the_next_expirys_carry():
    today = VALUATION.date().isoformat()  # about six hours left at 10:00
    later = "2026-11-03"
    T_later = mg.years_to_expiry(later, "SPY", VALUATION_MS)
    quotes = _quotes(exp=today)
    for K, call, put, _ in _parity_pairs(SPOT, T_later, r=0.03, q=0.0):
        quotes.append(("SPY", later, "C", K, call - 0.025, call + 0.025))
        quotes.append(("SPY", later, "P", K, put - 0.025, put + 0.025))
    quotes += _quotes()  # 2026-11-20 at r - q = 2.5%
    carries = mg.expiry_carries(quotes, SPOT, VALUATION_MS)
    assert carries[("SPY", later)] == pytest.approx(0.03, abs=1e-9)
    assert carries[("SPY", today)] == carries[("SPY", later)]  # the nearest later one
    # Alone, a same-day expiry gets no carry, and its rows fall back to the default rate.
    assert mg.expiry_carries(_quotes(exp=today), SPOT, VALUATION_MS) == {}
