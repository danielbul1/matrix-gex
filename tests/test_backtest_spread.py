"""Tests for the spread backtester (tools/backtest_spread.py).

Every test builds a small synthetic chain store and asserts on the report an
experiment run produces: which strike was sold, what the spread earned after
fills and commissions, and how many contracts the 1% max-loss rule allowed.
"""
import importlib.util
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
from tripity_experiment import chain_store as cs
from tripity_experiment import matrix_gex as mg

spec = importlib.util.spec_from_file_location(
    "backtest_spread", ROOT / "tools" / "backtest_spread.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

ET = mg.ET
MONDAY = date(2026, 3, 2)
SPOT = 5000.0
IV = 0.15
STRIKES = range(4700, 5025, 25)
# Spot 5000, IV 15%, 7 calendar days from 10:00 ET: the 4900 put is -0.155
# delta; its neighbours are -0.104 (4875) and -0.219 (4925).
SHORT, LONG = 4900.0, 4875.0

# Hand-checkable quotes for the two legs of the 16-delta / 25-wide spread.
# 50% fills: sell 4900 at 8.60 - 0.10 = 8.50, buy 4875 at 5.30 + 0.10 = 5.40.
LEG_QUOTES = {SHORT: (8.40, 8.80), LONG: (5.10, 5.50)}
CREDIT = 3.10
# IBKR tier ($0.65, premium >= $0.10) + Cboe SPXW customer fees for a
# premium >= $1 ($0.45 + $0.14 + $0.01248 + $0.0025 = $0.60498), per leg.
EXCHANGE_FEE = 0.60498
COST_PER_SPREAD = 2 * (0.65 + EXCHANGE_FEE)
MAX_LOSS_PER_SPREAD = (25 - CREDIT) * 100 + COST_PER_SPREAD  # 2192.51


def _ms(day, hour, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


def _chain(session, hour, minute, expiry_offsets=(4, 7, 9), leg_quotes=None,
           spot=SPOT):
    """A put chain for one snapshot. Every strike carries IV 15%; quotes are
    Black-Scholes +/- 0.05 except the legs pinned by leg_quotes (7DTE only)."""
    leg_quotes = LEG_QUOTES if leg_quotes is None else leg_quotes
    stamp = _ms(session, hour, minute)
    rows = []
    for offset in expiry_offsets:
        expiry = (session + timedelta(days=offset)).isoformat()
        T = mg.years_to_expiry(expiry, "SPXW", stamp)
        for strike in STRIKES:
            price = mg.bs_price(spot, strike, T, IV, is_call=False)
            bid, ask = max(price - 0.05, 0.05), price + 0.05
            if offset == 7 and strike in leg_quotes:
                bid, ask = leg_quotes[strike]
            rows.append(cs.ChainRow(
                root="SPXW", expiry=expiry, strike=float(strike), right="P",
                snapshot_ms=stamp, bid=bid, ask=ask, iv=IV, sod_oi=1000.0,
                vendor_delta=None, vendor_gamma=None, underlying=spot))
    return rows


def _store(tmp_path, sessions, spx_closes, vix_closes=None):
    """sessions: {session_date: [ChainRow, ...]}."""
    path = tmp_path / "chain.sqlite3"
    connection = cs.connect(path)
    for rows in sessions.values():
        cs.write_rows(connection, rows)
    cs.write_daily_closes(connection, "SPX",
                          {d.isoformat(): v for d, v in spx_closes.items()})
    cs.write_daily_closes(connection, "VIX", {
        d.isoformat(): v for d, v in (vix_closes or {}).items()})
    connection.close()
    return path


def _run(path, **overrides):
    config = bt.SpreadConfig(**{"initial_equity": 1_000_000.0, **overrides})
    return bt.run_experiment(path, config)


def _one_session_store(tmp_path, settlement, leg_quotes=None):
    expiry = MONDAY + timedelta(days=7)
    return _store(tmp_path, {MONDAY: _chain(MONDAY, 10, 0, leg_quotes=leg_quotes)},
                  {MONDAY: SPOT, expiry: settlement})


# ---------------------------------------------------------------------------
# Strike and expiry selection
# ---------------------------------------------------------------------------
def test_short_strike_is_picked_by_delta_from_snapshot_iv(tmp_path):
    report = _run(_one_session_store(tmp_path, settlement=5010.0))
    (trade,) = report["trades"]
    assert trade["short_strike"] == SHORT
    assert trade["long_strike"] == LONG
    assert trade["short_delta"] == pytest.approx(-0.155, abs=0.001)


def test_target_delta_changes_the_strike(tmp_path):
    report = _run(_one_session_store(tmp_path, settlement=5010.0),
                  short_delta=0.10)
    assert report["trades"][0]["short_strike"] == 4875.0


def test_expiry_nearest_seven_calendar_days_is_traded(tmp_path):
    report = _run(_one_session_store(tmp_path, settlement=5010.0))
    assert report["trades"][0]["expiry"] == "2026-03-09"


def test_entry_uses_the_10am_snapshot(tmp_path):
    # Poisoned quotes at 09:45 and 15:00 must not move the fills.
    poison = {SHORT: (40.0, 41.0), LONG: (0.10, 0.20)}
    rows = (_chain(MONDAY, 9, 45, leg_quotes=poison) + _chain(MONDAY, 10, 0)
            + _chain(MONDAY, 15, 0, leg_quotes=poison))
    path = _store(tmp_path, {MONDAY: rows},
                  {MONDAY: SPOT, MONDAY + timedelta(days=7): 5010.0})
    (trade,) = _run(path)["trades"]
    assert trade["credit"] == pytest.approx(CREDIT)


# ---------------------------------------------------------------------------
# Settlement P&L
# ---------------------------------------------------------------------------
def test_spread_expiring_otm_keeps_credit_minus_costs(tmp_path):
    report = _run(_one_session_store(tmp_path, settlement=5010.0))
    (trade,) = report["trades"]
    assert trade["contracts"] == 4
    assert trade["settlement"] == 5010.0
    assert trade["pnl"] == pytest.approx(4 * (CREDIT * 100 - COST_PER_SPREAD))


def test_spread_expiring_itm_loses_width_minus_credit_plus_costs(tmp_path):
    report = _run(_one_session_store(tmp_path, settlement=4850.0))
    (trade,) = report["trades"]
    assert trade["pnl"] == pytest.approx(
        -4 * ((25 - CREDIT) * 100 + COST_PER_SPREAD))


def test_spread_settling_between_strikes_loses_intrinsic(tmp_path):
    report = _run(_one_session_store(tmp_path, settlement=4890.0))
    (trade,) = report["trades"]
    # The short 4900 put settles at 10.00, the long 4875 put at zero.
    assert trade["pnl"] == pytest.approx(4 * ((CREDIT - 10) * 100 - COST_PER_SPREAD))


# ---------------------------------------------------------------------------
# Fills and commissions
# ---------------------------------------------------------------------------
def test_fills_cross_half_the_quoted_spread(tmp_path):
    (trade,) = _run(_one_session_store(tmp_path, settlement=5010.0))["trades"]
    assert trade["short_fill"] == pytest.approx(8.50)
    assert trade["long_fill"] == pytest.approx(5.40)
    assert trade["credit"] == pytest.approx(CREDIT)
    assert trade["costs_per_spread"] == pytest.approx(COST_PER_SPREAD)


def test_mid_and_full_spread_fills(tmp_path):
    path = _one_session_store(tmp_path, settlement=5010.0)
    assert _run(path, fill_ratio=0.0)["trades"][0]["credit"] == pytest.approx(3.30)
    assert _run(path, fill_ratio=1.0)["trades"][0]["credit"] == pytest.approx(2.90)


def test_cheap_wing_pays_the_lower_commission_tier(tmp_path):
    # Long leg fills at 0.07 + 0.01 = 0.08 -> the $0.50 tier (premium < $0.10)
    # and the $0.36 Cboe fee (premium < $1): 0.36 + 0.14 + 0.01248 + 0.0025.
    quotes = {SHORT: (8.40, 8.80), LONG: (0.05, 0.09)}
    (trade,) = _run(_one_session_store(tmp_path, 5010.0, leg_quotes=quotes))["trades"]
    assert trade["long_fill"] == pytest.approx(0.08)
    assert trade["costs_per_spread"] == pytest.approx(
        (0.65 + EXCHANGE_FEE) + (0.50 + 0.51498))


def test_wing_under_one_dollar_pays_the_lower_exchange_fee(tmp_path):
    # Long leg fills at 0.80 + 0.10 = 0.90: full $0.65 commission, but the
    # Cboe fee for a premium < $1.
    quotes = {SHORT: (8.40, 8.80), LONG: (0.60, 1.00)}
    (trade,) = _run(_one_session_store(tmp_path, 5010.0, leg_quotes=quotes))["trades"]
    assert trade["long_fill"] == pytest.approx(0.90)
    assert trade["costs_per_spread"] == pytest.approx(
        (0.65 + EXCHANGE_FEE) + (0.65 + 0.51498))


def test_single_spread_pays_the_one_dollar_order_minimum_per_leg(tmp_path):
    # 1% of 250,000 = 2,500 -> one spread; 1 x $0.65 is below IBKR's $1.00
    # order minimum, which applies to each leg of the combo.
    path = _one_session_store(tmp_path, settlement=5010.0)
    (trade,) = _run(path, initial_equity=250_000.0)["trades"]
    cost = 2 * (1.00 + EXCHANGE_FEE)
    assert trade["contracts"] == 1
    assert trade["costs_per_spread"] == pytest.approx(cost)
    assert trade["pnl"] == pytest.approx(CREDIT * 100 - cost)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
def test_contracts_sized_so_max_loss_is_one_percent_of_equity(tmp_path):
    path = _one_session_store(tmp_path, settlement=5010.0)
    # 1% of 880,000 = 8,800 -> floor(8800 / 2192.50) = 4 spreads.
    (trade,) = _run(path, initial_equity=880_000.0)["trades"]
    assert trade["max_loss_per_spread"] == pytest.approx(MAX_LOSS_PER_SPREAD)
    assert trade["contracts"] == 4
    assert trade["contracts"] * MAX_LOSS_PER_SPREAD <= 8_800


def test_sizing_uses_equity_after_settled_trades(tmp_path):
    first_expiry = MONDAY + timedelta(days=7)
    later = MONDAY + timedelta(days=8)  # Tuesday after the first expiry
    path = _store(tmp_path, {
        MONDAY: _chain(MONDAY, 10, 0),
        later: _chain(later, 10, 0),
    }, {MONDAY: SPOT, first_expiry: 4850.0, later: SPOT,
        later + timedelta(days=7): 5010.0})
    report = _run(path, initial_equity=880_000.0)
    first, second = report["trades"]
    assert first["contracts"] == 4
    loss = 4 * MAX_LOSS_PER_SPREAD
    assert first["pnl"] == pytest.approx(-loss)
    # 1% of (880,000 - 8,770) = 8,712.30 -> only 3 spreads fit.
    assert second["equity_at_entry"] == pytest.approx(880_000 - loss)
    assert second["contracts"] == 3


def test_order_minimum_counts_toward_the_risk_budget(tmp_path):
    # 1% of 219,290 = 2,192.90: enough for one spread at $0.65 a leg
    # (2,192.51), not at the $1.00 order minimum (2,193.21).
    report = _run(_one_session_store(tmp_path, settlement=5010.0),
                  initial_equity=219_290.0)
    assert report["trades"] == []


def test_equity_too_small_for_one_spread_is_skipped(tmp_path):
    report = _run(_one_session_store(tmp_path, settlement=5010.0),
                  initial_equity=100_000.0)
    assert report["trades"] == []
    assert report["skipped"] == [
        {"session": "2026-03-02", "reason": "max loss of one spread exceeds risk budget"}]


# ---------------------------------------------------------------------------
# Report over a multi-session store, via the CLI
# ---------------------------------------------------------------------------
def test_cli_writes_json_report_over_multi_session_store(tmp_path, capsys):
    sessions, closes = {}, {}
    settlements = [5010.0, 4850.0, 5020.0, 4990.0, 4930.0]
    for i, settle in enumerate(settlements):
        session = MONDAY + timedelta(days=i)
        sessions[session] = _chain(session, 10, 0)
        closes[session] = SPOT
        closes[session + timedelta(days=7)] = settle
    path = _store(tmp_path, sessions, closes)
    out = tmp_path / "report.json"

    code = bt.main(["--store", str(path), "--equity", "1000000", "--out", str(out)])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["config"]["short_delta"] == 0.16
    assert report["config"]["width"] == 25.0
    metrics = report["metrics"]
    assert metrics["trades"] == 5
    assert set(metrics) >= {"sharpe", "sortino", "max_drawdown", "worst_week",
                            "cvar_5", "win_rate", "avg_pnl_per_trade", "trades"}
    pnls = [t["pnl"] for t in report["trades"]]
    assert metrics["win_rate"] == pytest.approx(4 / 5)
    assert metrics["avg_pnl_per_trade"] == pytest.approx(sum(pnls) / 5)
    # Ten days (five sessions, five expiries): the worst 5% is one day, the
    # 4850 loss settling after the first trade's win.
    assert metrics["cvar_5"] == pytest.approx(pnls[1] / (1_000_000 + pnls[0]))
    # Every expiry settles in the same week, whose net P&L is a loss.
    assert sum(pnls) < 0
    assert metrics["worst_week"] == pytest.approx(sum(pnls) / 1_000_000)
    assert metrics["max_drawdown"] < 0
    assert math.isfinite(metrics["sharpe"]) and math.isfinite(metrics["sortino"])
    assert json.loads(capsys.readouterr().out) == report


def test_cli_reports_missing_store(tmp_path, capsys):
    code = bt.main(["--store", str(tmp_path / "absent.sqlite3")])
    assert code == 2
    assert "not found" in capsys.readouterr().err
