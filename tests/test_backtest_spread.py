"""Tests for the spread backtester (tools/backtest_spread.py).

Every test builds a small synthetic chain store and asserts on the report an
experiment run produces: which strike was sold, what the spread earned after
fills and commissions, and how many contracts the 1% max-loss rule allowed.
"""
import importlib.util
import json
import math
import random
import statistics
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
    """sessions: {session_date: [ChainRow, ...]}. Without vix_closes, VIX
    closes at 20 the day before every session."""
    if vix_closes is None:
        vix_closes = {day - timedelta(days=1): 20.0 for day in sessions}
    path = tmp_path / "chain.sqlite3"
    connection = cs.connect(path)
    for rows in sessions.values():
        cs.write_rows(connection, rows)
    cs.write_daily_closes(connection, "SPX",
                          {d.isoformat(): v for d, v in spx_closes.items()})
    cs.write_daily_closes(connection, "VIX", {
        d.isoformat(): v for d, v in vix_closes.items()})
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
# Managed exits: 50% take-profit / 2x credit stop on the intraday grid
# ---------------------------------------------------------------------------
EXPIRY = MONDAY + timedelta(days=7)


def _legs(day, hour, minute, short_quote, long_quote, expiry=EXPIRY):
    """Only the two legs of the 4900/4875 spread, quoted at one snapshot."""
    stamp = _ms(day, hour, minute)
    return [cs.ChainRow(root="SPXW", expiry=expiry.isoformat(), strike=strike,
                        right="P", snapshot_ms=stamp, bid=bid, ask=ask, iv=IV,
                        sod_oi=1000.0, vendor_delta=None, vendor_gamma=None,
                        underlying=SPOT)
            for strike, (bid, ask) in ((SHORT, short_quote), (LONG, long_quote))]


def _managed_store(tmp_path, later_snapshots, settlement=5010.0):
    """Monday's 10:00 entry plus leg-only snapshots: {day: [rows, ...]}."""
    sessions = {MONDAY: _chain(MONDAY, 10, 0)}
    for day, rows in later_snapshots.items():
        sessions[day] = sessions.get(day, []) + rows
    return _store(tmp_path, sessions, {MONDAY: SPOT, EXPIRY: settlement})


def _exit_costs(short_fill, long_fill):
    return sum(0.65 + (EXCHANGE_FEE if fill >= 1 else 0.51498)
               for fill in (short_fill, long_fill))


# Credit 3.10: take profit when the buy-back debit is <= 1.55, stop at >= 6.20.
# Debits below are at 50% fills: buy the short leg at mid + 0.10, sell the long
# leg at mid - 0.10 (both quotes are 0.20 wide).
NEAR_TP = ((2.10, 2.30), (0.60, 0.80))   # 2.25 - 0.65 = 1.60
AT_TP = ((2.00, 2.20), (0.60, 0.80))     # 2.15 - 0.65 = 1.50
DEEP_TP = ((1.00, 1.20), (0.30, 0.50))   # 1.15 - 0.35 = 0.80
NEAR_STOP = ((13.80, 14.20), (7.90, 8.30))  # 14.10 - 8.00 = 6.10
AT_STOP = ((14.00, 14.40), (7.90, 8.30))    # 14.30 - 8.00 = 6.30


def test_take_profit_triggers_at_first_snapshot_at_half_the_credit(tmp_path):
    path = _managed_store(tmp_path, {MONDAY: (
        _legs(MONDAY, 10, 30, *NEAR_TP) + _legs(MONDAY, 11, 0, *AT_TP)
        + _legs(MONDAY, 11, 30, *DEEP_TP))})
    (trade,) = _run(path, exit_rule="managed")["trades"]
    assert trade["exit"] == "take_profit"
    assert trade["exit_time"] == "2026-03-02T11:00:00-05:00"
    assert trade["closed"] == "2026-03-02"
    assert trade["exit_debit"] == pytest.approx(1.50)
    costs = COST_PER_SPREAD + _exit_costs(2.15, 0.65)
    assert trade["costs_per_spread"] == pytest.approx(costs)
    assert trade["pnl"] == pytest.approx(4 * ((CREDIT - 1.50) * 100 - costs))


def test_stop_triggers_at_first_snapshot_costing_twice_the_credit(tmp_path):
    tuesday = MONDAY + timedelta(days=1)
    path = _managed_store(tmp_path, {
        MONDAY: _legs(MONDAY, 15, 30, *NEAR_STOP),
        tuesday: _legs(tuesday, 11, 0, *AT_STOP) + _legs(tuesday, 11, 30, *NEAR_STOP),
    }, settlement=4850.0)
    (trade,) = _run(path, exit_rule="managed")["trades"]
    assert trade["exit"] == "stop"
    assert trade["exit_time"] == "2026-03-03T11:00:00-05:00"
    # The stop pays the same 50% of the spread as the entry, not mid (6.20).
    assert trade["exit_debit"] == pytest.approx(6.30)
    costs = COST_PER_SPREAD + _exit_costs(14.30, 8.00)
    assert trade["pnl"] == pytest.approx(4 * ((CREDIT - 6.30) * 100 - costs))


def test_stop_fills_move_with_the_fill_ratio(tmp_path):
    tuesday = MONDAY + timedelta(days=1)
    path = _managed_store(tmp_path, {tuesday: _legs(tuesday, 11, 0, *AT_STOP)})
    # Full spread: credit 2.90, buy back at 14.40 - 7.90 = 6.50 (>= 5.80).
    (trade,) = _run(path, exit_rule="managed", fill_ratio=1.0)["trades"]
    assert trade["exit"] == "stop"
    assert trade["exit_debit"] == pytest.approx(6.50)


def test_spread_reaching_neither_is_held_to_expiry(tmp_path):
    tuesday = MONDAY + timedelta(days=1)
    path = _managed_store(tmp_path, {
        MONDAY: _legs(MONDAY, 12, 0, *NEAR_TP),
        tuesday: _legs(tuesday, 12, 0, *NEAR_STOP),
        EXPIRY: _legs(EXPIRY, 15, 30, *NEAR_TP),
    }, settlement=4890.0)
    (trade,) = _run(path, exit_rule="managed")["trades"]
    assert trade["exit"] == "expiry"
    assert trade["closed"] == EXPIRY.isoformat()
    assert trade["pnl"] == pytest.approx(4 * ((CREDIT - 10) * 100 - COST_PER_SPREAD))


def test_hold_variant_ignores_take_profit_snapshots(tmp_path):
    path = _managed_store(tmp_path, {MONDAY: _legs(MONDAY, 11, 0, *DEEP_TP)})
    (trade,) = _run(path)["trades"]
    assert trade["exit"] == "expiry"
    assert trade["pnl"] == pytest.approx(4 * (CREDIT * 100 - COST_PER_SPREAD))


def test_snapshots_with_a_bad_quote_are_not_exits(tmp_path):
    crossed = ((2.20, 2.00), (0.60, 0.80))
    path = _managed_store(tmp_path, {MONDAY: (
        _legs(MONDAY, 10, 30, *crossed) + _legs(MONDAY, 11, 0, *AT_TP))})
    (trade,) = _run(path, exit_rule="managed")["trades"]
    assert trade["exit_time"] == "2026-03-02T11:00:00-05:00"


# ---------------------------------------------------------------------------
# At most five open positions
# ---------------------------------------------------------------------------
def _week_then_monday_store(tmp_path, extra_rows=None):
    """Entries Monday to Friday and the next Monday, all settling OTM.
    extra_rows: {day: [rows, ...]} added to those sessions."""
    days = [MONDAY + timedelta(days=d) for d in (0, 1, 2, 3, 4, 7)]
    closes = {day: SPOT for day in days}
    closes.update({day + timedelta(days=7): 5010.0 for day in days})
    sessions = {day: _chain(day, 10, 0) + (extra_rows or {}).get(day, [])
                for day in days}
    return days, _store(tmp_path, sessions, closes)


def test_no_more_than_five_positions_are_open_at_once(tmp_path):
    # Mon-Fri entries all expire the following week; the next Monday's 10:00
    # entry would be a sixth position while the first still awaits settlement.
    days, path = _week_then_monday_store(tmp_path)
    report = _run(path)
    assert [t["session"] for t in report["trades"]] == [d.isoformat() for d in days[:5]]
    assert report["skipped"] == [
        {"session": "2026-03-09", "reason": "position cap reached"}]


def test_a_position_closed_early_frees_a_slot(tmp_path):
    # Monday's spread takes profit on Tuesday, before the next Monday.
    tuesday = MONDAY + timedelta(days=1)
    _, path = _week_then_monday_store(
        tmp_path, {tuesday: _legs(tuesday, 12, 0, *AT_TP)})
    report = _run(path, exit_rule="managed")
    assert len(report["trades"]) == 6
    assert report["trades"][0]["closed"] == "2026-03-03"


# ---------------------------------------------------------------------------
# The parameter grid and fill sensitivity
# ---------------------------------------------------------------------------
def _five_session_store(tmp_path, settlements, expiry_offsets=(4, 7, 9)):
    sessions, closes = {}, {}
    for i, settle in enumerate(settlements):
        session = MONDAY + timedelta(days=i)
        sessions[session] = _chain(session, 10, 0, expiry_offsets=expiry_offsets)
        closes[session] = SPOT
        closes[session + timedelta(days=7)] = settle
    return _store(tmp_path, sessions, closes)


def test_grid_report_has_one_row_per_cell_and_a_sensitivity_table(tmp_path, capsys):
    path = _five_session_store(tmp_path, [5010.0, 4850.0, 5020.0, 4990.0, 4930.0])
    out = tmp_path / "grid.json"
    assert bt.main(["--store", str(path), "--grid", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    cells = {(r["short_delta"], r["width"], r["exit_rule"]) for r in report["headline"]}
    assert cells == {(d, w, e) for d in (0.10, 0.16, 0.20) for w in (25.0, 50.0)
                     for e in ("hold", "managed")}
    assert len(report["headline"]) == 12
    assert {r["fill"] for r in report["headline"]} == {"50%"}
    assert all(r["trades"] == 5 for r in report["headline"])
    sensitivity = {(r["short_delta"], r["width"], r["exit_rule"], r["fill"])
                   for r in report["fill_sensitivity"]}
    assert sensitivity == {c + (f,) for c in cells for f in ("mid", "50%", "full")}
    assert len(report["fill_sensitivity"]) == 36
    assert json.loads(capsys.readouterr().out) == report


def test_headline_cell_matches_a_single_run(tmp_path):
    path = _five_session_store(tmp_path, [5010.0, 4850.0, 5020.0, 4990.0, 4930.0])
    report = bt.run_grid(path, initial_equity=1_000_000.0)
    (row,) = [r for r in report["headline"] if (r["short_delta"], r["width"],
                                                r["exit_rule"]) == (0.16, 25.0, "hold")]
    single = _run(path)["metrics"]
    assert {k: row[k] for k in single} == single


def test_pnl_is_ordered_mid_then_half_spread_then_full(tmp_path):
    # Winners only, and no session re-marks an earlier session's expiry, so
    # every fill level exits the same way. (A take-profit is a share of the
    # credit, so on other paths mid can take profit where 50% holds on and
    # keeps more.)
    path = _five_session_store(tmp_path, [5010.0] * 5, expiry_offsets=(7,))
    report = bt.run_grid(path, initial_equity=1_000_000.0)
    pnl = {(r["short_delta"], r["width"], r["exit_rule"], r["fill"]): r["total_pnl"]
           for r in report["fill_sensitivity"]}
    for delta in (0.10, 0.16, 0.20):
        for width in (25.0, 50.0):
            for rule in ("hold", "managed"):
                cell = (delta, width, rule)
                assert pnl[cell + ("mid",)] >= pnl[cell + ("50%",)] >= pnl[cell + ("full",)]
                assert pnl[cell + ("mid",)] > pnl[cell + ("full",)]


def test_take_profit_pnl_is_ordered_across_fill_levels(tmp_path):
    # Every fill level takes profit at the same snapshot.
    path = _managed_store(tmp_path, {MONDAY: _legs(MONDAY, 11, 0, *DEEP_TP)})
    runs = [_run(path, exit_rule="managed", fill_ratio=ratio)["trades"][0]
            for ratio in (0.0, 0.5, 1.0)]
    assert [t["exit"] for t in runs] == ["take_profit"] * 3
    mid, half, full = (t["pnl"] for t in runs)
    assert mid > half > full


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


def test_cli_runs_the_managed_exit_rule(tmp_path):
    path = _managed_store(tmp_path, {MONDAY: _legs(MONDAY, 11, 0, *AT_TP)})
    out = tmp_path / "report.json"
    assert bt.main(["--store", str(path), "--exit-rule", "managed",
                    "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["config"]["exit_rule"] == "managed"
    assert report["trades"][0]["exit"] == "take_profit"


def test_cli_grid_rejects_single_cell_flags(tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        bt.main(["--store", str(tmp_path / "chain.sqlite3"), "--grid",
                 "--width", "50"])
    assert raised.value.code == 2
    assert "--width" in capsys.readouterr().err


def test_cli_reports_missing_store(tmp_path, capsys):
    code = bt.main(["--store", str(tmp_path / "absent.sqlite3")])
    assert code == 2
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The GEX Filter: Naive GEX, Gamma Regime, GEX Percentile, Rejected Sessions
# ---------------------------------------------------------------------------
def _option(session, hour, minute, right, strike, oi, expiry_offset=7,
            root="SPXW", iv=IV, spot=SPOT):
    """One contract carrying only what Naive GEX reads: SOD OI, IV, spot."""
    return cs.ChainRow(
        root=root, expiry=(session + timedelta(days=expiry_offset)).isoformat(),
        strike=float(strike), right=right, snapshot_ms=_ms(session, hour, minute),
        bid=1.00, ask=1.10, iv=iv, sod_oi=float(oi), vendor_delta=None,
        vendor_gamma=None, underlying=spot)


def _hand_gex(session, hour, minute, contracts):
    """Naive GEX by hand from canonical gamma: calls +, puts -."""
    stamp = _ms(session, hour, minute)
    total = 0.0
    for right, strike, oi, offset, root in contracts:
        expiry = (session + timedelta(days=offset)).isoformat()
        T = mg.years_to_expiry(expiry, root, stamp)
        gamma = mg.bs_gamma(SPOT, strike, T, IV)
        total += (1 if right == "C" else -1) * gamma * oi * 100 * SPOT ** 2 * 0.01
    return total


def _gex_run(path, **overrides):
    config = bt.SpreadConfig(**{"initial_equity": 1_000_000.0, **overrides})
    return bt.run_gex_filter(path, config)


def _gex_of(report, session):
    (row,) = [r for r in report["gex"] if r["session"] == session.isoformat()]
    return row


def test_naive_gex_matches_the_canonical_engine_by_hand(tmp_path):
    # SPXW and AM-settled SPX alike, 0DTE included, from SOD OI at 10:00.
    contracts = [("C", 5050.0, 2000, 7, "SPXW"), ("P", 4900.0, 1500, 7, "SPXW"),
                 ("C", 5000.0, 800, 4, "SPX"), ("P", 4975.0, 300, 0, "SPXW")]
    rows = [_option(MONDAY, 10, 0, right, strike, oi, offset, root)
            for right, strike, oi, offset, root in contracts]
    path = _store(tmp_path, {MONDAY: rows}, {MONDAY: SPOT})
    row = _gex_of(_gex_run(path), MONDAY)
    assert row["naive_gex"] == pytest.approx(_hand_gex(MONDAY, 10, 0, contracts))
    assert row["gamma_regime"] == "positive"


def _gamma_calls(session, oi=50_000):
    """Call OI heavy enough to turn a put chain's Naive GEX positive."""
    return [_option(session, 10, 0, "C", strike, oi) for strike in (5000, 5025, 5050)]


def _positive_then_negative_store(tmp_path):
    """Monday: positive Gamma Regime; Tuesday: negative. Both settle OTM."""
    tuesday = MONDAY + timedelta(days=1)
    sessions = {MONDAY: _chain(MONDAY, 10, 0) + _gamma_calls(MONDAY),
                tuesday: _chain(tuesday, 10, 0)}
    closes = {MONDAY: SPOT, tuesday: SPOT, MONDAY + timedelta(days=7): 5010.0,
              tuesday + timedelta(days=7): 5010.0}
    return tuesday, _store(tmp_path, sessions, closes)


def _filter(report, name):
    (variant,) = [f for f in report["filters"] if f["filter"] == name]
    return {row["sessions"]: row for row in variant["rows"]}


def test_positive_gamma_filter_reports_kept_rejected_and_baseline(tmp_path):
    tuesday, path = _positive_then_negative_store(tmp_path)
    report = _gex_run(path)
    assert _gex_of(report, MONDAY)["gamma_regime"] == "positive"
    assert _gex_of(report, tuesday)["gamma_regime"] == "negative"
    rows = _filter(report, "positive_gamma")
    assert set(rows) == {"kept", "rejected", "baseline", "iv_matched_control"}
    assert [t["session"] for t in rows["kept"]["trades"]] == ["2026-03-02"]
    assert [t["session"] for t in rows["rejected"]["trades"]] == ["2026-03-03"]
    assert [t["session"] for t in rows["baseline"]["trades"]] == [
        "2026-03-02", "2026-03-03"]
    assert rows["kept"]["session_count"] == 1
    assert rows["rejected"]["session_count"] == 1
    assert rows["baseline"]["session_count"] == 2
    assert rows["kept"]["metrics"]["trades"] == 1
    assert rows["baseline"]["metrics"]["total_pnl"] == pytest.approx(
        rows["kept"]["metrics"]["total_pnl"] + rows["rejected"]["metrics"]["total_pnl"])


def _percentile_store(tmp_path, later_oi=1_000_000, extra_rows=()):
    """Twenty sessions of rising call OI, then T at the middle of them, then
    T+1 far above everything. Naive GEX is proportional to the OI.
    extra_rows join T's session."""
    days = [MONDAY + timedelta(days=i) for i in range(22)]
    ois = [1000 * (i + 1) for i in range(20)] + [10_500, later_oi]
    sessions = {day: [_option(day, 10, 0, "C", 5000, oi)] for day, oi in zip(days, ois)}
    sessions[days[20]] += list(extra_rows)
    return days, _store(tmp_path, sessions, {})


def test_gex_percentile_ranks_against_past_sessions_only(tmp_path):
    days, path = _percentile_store(tmp_path)
    report = _gex_run(path)
    # 10 of the 20 past sessions sit below T; T itself and T+1 do not count.
    assert _gex_of(report, days[20])["gex_percentile"] == pytest.approx(0.5)
    assert _gex_of(report, days[21])["gex_percentile"] == pytest.approx(1.0)
    # Fewer than 20 past sessions: no percentile yet.
    assert _gex_of(report, days[19])["gex_percentile"] is None


def test_gex_percentile_is_unchanged_by_later_sessions(tmp_path):
    days, path = _percentile_store(tmp_path, later_oi=1)
    assert _gex_of(_gex_run(path), days[20])["gex_percentile"] == pytest.approx(0.5)


def test_gex_percentile_history_reaches_before_the_start_date(tmp_path):
    days, path = _percentile_store(tmp_path)
    report = bt.run_gex_filter(path, bt.SpreadConfig(), start=days[20])
    assert _gex_of(report, days[20])["gex_percentile"] == pytest.approx(0.5)


def test_percentile_filter_keeps_sessions_above_the_threshold(tmp_path):
    days, path = _percentile_store(tmp_path)
    (variant,) = [f for f in _gex_run(path)["filters"] if f["filter"] == "gex_percentile"]
    assert variant["threshold"] == 0.5
    rows = {row["sessions"]: row for row in variant["rows"]}
    # T sits at exactly 0.5 (not above); T+1 is above; Undecided Sessions
    # are neither kept, rejected, nor in the Baseline row.
    assert rows["kept"]["session_count"] == 1
    assert rows["rejected"]["session_count"] == 1
    assert rows["baseline"]["session_count"] == 2
    assert variant["undecided"] == [
        {"session": day.isoformat(), "reason": "fewer than 20 past sessions of Naive GEX"}
        for day in days[:20]]


ENTRY_FIELDS = ("session", "expiry", "short_strike", "long_strike", "short_delta",
                "underlying_at_entry", "short_fill", "long_fill", "credit", "contracts")


def _monday_view(report):
    """Monday's Naive GEX, filter decision and entry."""
    rows = _filter(report, "positive_gamma")
    (trade,) = [t for t in rows["kept"]["trades"] if t["session"] == "2026-03-02"]
    return (_gex_of(report, MONDAY), {f: trade[f] for f in ENTRY_FIELDS})


def test_no_look_ahead_from_next_day_oi_or_later_quotes(tmp_path):
    tuesday = MONDAY + timedelta(days=1)
    expiry = MONDAY + timedelta(days=7)
    closes = {MONDAY: SPOT, tuesday: SPOT, expiry: 5010.0,
              tuesday + timedelta(days=7): 5010.0}
    clean = {MONDAY: _chain(MONDAY, 10, 0) + _gamma_calls(MONDAY),
             tuesday: _chain(tuesday, 10, 0)}
    # T+1 open interest poisoned: huge put OI at every Tuesday strike.
    poisoned_tuesday = [
        cs.ChainRow(**{**row.__dict__, "sod_oi": 9e9}) for row in clean[tuesday]]
    # Every Monday quote after 10:00 poisoned: other spot, IV, OI and prices.
    later = []
    for hour, minute in ((10, 30), (12, 0), (15, 0)):
        for row in _chain(MONDAY, hour, minute, spot=4000.0):
            later.append(cs.ChainRow(**{
                **row.__dict__, "sod_oi": 9e9, "iv": 0.9, "bid": 90.0, "ask": 99.0}))
    poisoned = {MONDAY: clean[MONDAY] + later, tuesday: poisoned_tuesday}
    (tmp_path / "clean").mkdir()
    (tmp_path / "poisoned").mkdir()
    clean_report = _gex_run(_store(tmp_path / "clean", clean, closes))
    poisoned_report = _gex_run(_store(tmp_path / "poisoned", poisoned, closes))
    assert _monday_view(poisoned_report) == _monday_view(clean_report)
    # The poison was read: Tuesday's own Naive GEX moved with it.
    assert _gex_of(poisoned_report, tuesday)["naive_gex"] != pytest.approx(
        _gex_of(clean_report, tuesday)["naive_gex"])


def _percentile_view(report, session):
    """The session's Naive GEX and GEX Percentile, and the percentile
    filter's split: T rejected at exactly 0.5, T+1 kept."""
    rows = _filter(report, "gex_percentile")
    return (_gex_of(report, session),
            rows["kept"]["session_count"], rows["rejected"]["session_count"])


def test_no_look_ahead_in_the_percentile_decision(tmp_path):
    # T+1 open interest poisoned, and T quoted again after 10:00 with call OI
    # that would lift T to the top of its history if it leaked in.
    later = [_option(MONDAY + timedelta(days=20), hour, minute, "C", 5000, 9e9,
                     iv=0.9, spot=4000.0)
             for hour, minute in ((10, 30), (12, 0), (15, 0))]
    (tmp_path / "clean").mkdir()
    (tmp_path / "poisoned").mkdir()
    days, clean_path = _percentile_store(tmp_path / "clean")
    _, poisoned_path = _percentile_store(tmp_path / "poisoned", later_oi=9e9,
                                         extra_rows=later)
    clean_report, poisoned_report = _gex_run(clean_path), _gex_run(poisoned_path)
    assert _percentile_view(poisoned_report, days[20]) == _percentile_view(
        clean_report, days[20])
    assert _gex_of(poisoned_report, days[21])["naive_gex"] > _gex_of(
        clean_report, days[21])["naive_gex"]


def test_cli_runs_the_gex_filter_report(tmp_path, capsys):
    _, path = _positive_then_negative_store(tmp_path)
    out = tmp_path / "gex.json"
    assert bt.main(["--store", str(path), "--gex-filter", "--gex-percentile", "0.8",
                    "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert [f["filter"] for f in report["filters"]] == ["positive_gamma", "gex_percentile"]
    assert report["filters"][1]["threshold"] == 0.8
    assert json.loads(capsys.readouterr().out) == report


def test_cli_gex_filter_and_grid_are_exclusive(tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        bt.main(["--store", str(tmp_path / "chain.sqlite3"), "--grid", "--gex-filter"])
    assert raised.value.code == 2


# ---------------------------------------------------------------------------
# The IV-Matched Control, the regression, the bootstrap and the verdict
# ---------------------------------------------------------------------------
def _comparison(report, name):
    (variant,) = [f for f in report["filters"] if f["filter"] == name]
    return variant["comparison"]


def test_iv_matched_control_trades_the_lowest_vix_sessions(tmp_path):
    # Monday is kept (positive Gamma Regime) at VIX 25; Tuesday is rejected at
    # VIX 15. The control trades one session too: the one with the lower VIX.
    tuesday = MONDAY + timedelta(days=1)
    sessions = {MONDAY: _chain(MONDAY, 10, 0) + _gamma_calls(MONDAY),
                tuesday: _chain(tuesday, 10, 0)}
    closes = {MONDAY: SPOT, tuesday: SPOT, MONDAY + timedelta(days=7): 5010.0,
              tuesday + timedelta(days=7): 5010.0}
    # VIX closes are read from the session before: Monday's close is Tuesday's.
    vix = {MONDAY - timedelta(days=1): 25.0, MONDAY: 15.0, tuesday: 99.0}
    report = _gex_run(_store(tmp_path, sessions, closes, vix))
    rows = _filter(report, "positive_gamma")
    control = rows["iv_matched_control"]
    assert control["session_count"] == rows["kept"]["session_count"] == 1
    assert control["vix_threshold"] == 15.0
    assert [t["session"] for t in control["trades"]] == ["2026-03-03"]


def test_session_without_a_vix_close_before_it_is_undecided(tmp_path):
    tuesday = MONDAY + timedelta(days=1)
    sessions = {MONDAY: _chain(MONDAY, 10, 0) + _gamma_calls(MONDAY),
                tuesday: _chain(tuesday, 10, 0)}
    closes = {MONDAY: SPOT, tuesday: SPOT, MONDAY + timedelta(days=7): 5010.0,
              tuesday + timedelta(days=7): 5010.0}
    # Only Tuesday's VIX (Monday's close) is known; Monday's same-day close
    # must not stand in for Monday itself.
    report = _gex_run(_store(tmp_path, sessions, closes, {MONDAY: 18.0}))
    (variant,) = [f for f in report["filters"] if f["filter"] == "positive_gamma"]
    assert variant["undecided"] == [
        {"session": "2026-03-02", "reason": "no VIX close before the session"}]
    rows = _filter(report, "positive_gamma")
    assert rows["baseline"]["session_count"] == 1


# Two years of weekly sessions. Each session's short 4900 put settles `loss`
# points in the money (never past the 4875 long leg), so its P&L falls
# linearly with loss.
FIRST_WEEK = date(2024, 1, 1)
WEEKS = 104


def _weekly_store(tmp_path, weeks):
    """weeks: [(vix, positive_gamma, loss)], one per weekly session."""
    days = [FIRST_WEEK + timedelta(weeks=i) for i in range(len(weeks))]
    sessions, vix = {}, {}
    for day, (vix_close, positive, _) in zip(days, weeks):
        sessions[day] = _chain(day, 10, 0, expiry_offsets=(7,)) + (
            _gamma_calls(day) if positive else [])
        vix[day - timedelta(days=1)] = vix_close
    # SPX wiggles every calendar day, so realized vol (and VRP) varies.
    closes = {}
    day, i = days[0] - timedelta(days=40), 0
    while day <= days[-1] + timedelta(days=7):
        closes[day] = SPOT + 5 * ((i * 7) % 11 - 5)
        day, i = day + timedelta(days=1), i + 1
    for day, (_, _, loss) in zip(days, weeks):
        closes[day + timedelta(days=7)] = SHORT - loss
    return _store(tmp_path, sessions, closes, vix)


@pytest.fixture(scope="module")
def vix_only_report(tmp_path_factory):
    """The GEX Filter is a pure function of VIX (positive Gamma Regime exactly
    when VIX < 20) and P&L depends on VIX alone."""
    rng = random.Random(1)
    weeks = []
    for _ in range(WEEKS):
        vix = rng.uniform(12, 30)
        weeks.append((vix, vix < 20, 0.3 * (vix - 12) + rng.uniform(0, 1)))
    return _gex_run(_weekly_store(tmp_path_factory.mktemp("vix_only"), weeks))


@pytest.fixture(scope="module")
def planted_report(tmp_path_factory):
    """Gamma Regime is independent of VIX, and negative gamma sessions lose
    five more points on top of a VIX-driven loss."""
    rng = random.Random(2)
    weeks = []
    for _ in range(WEEKS):
        vix, positive = rng.uniform(12, 30), rng.random() < 0.5
        weeks.append((vix, positive, 0.1 * (vix - 12) + rng.uniform(0, 1)
                      + (0 if positive else 5)))
    return _gex_run(_weekly_store(tmp_path_factory.mktemp("planted"), weeks))


@pytest.mark.parametrize("name", ["positive_gamma", "gex_percentile"])
def test_iv_matched_control_trades_as_many_sessions_as_its_filter(
        vix_only_report, planted_report, name):
    for report in (vix_only_report, planted_report):
        rows = _filter(report, name)
        assert rows["kept"]["session_count"] > 0
        assert rows["iv_matched_control"]["session_count"] == rows["kept"]["session_count"]


def test_filter_that_is_a_function_of_vix_adds_nothing(vix_only_report):
    comparison = _comparison(vix_only_report, "positive_gamma")
    regression = comparison["regression"]
    pnls = [t["pnl"] for t in _filter(vix_only_report, "positive_gamma")["baseline"]["trades"]]
    assert regression["trades"] == len(pnls)
    assert abs(regression["filter_coef"]) < 0.25 * statistics.stdev(pnls)
    low, high = regression["filter_ci"]
    assert low <= 0 <= high
    for measure in ("sharpe", "mean_pnl"):
        low, high = comparison["bootstrap"][measure]["ci"]
        assert low <= 0 <= high
    assert comparison["verdict"] == "null"
    assert "adds nothing beyond VIX" in vix_only_report["verdict"]


def test_planted_incremental_signal_yields_the_positive_verdict(planted_report):
    comparison = _comparison(planted_report, "positive_gamma")
    assert comparison["regression"]["filter_ci"][0] > 0
    for measure in ("sharpe", "mean_pnl"):
        assert comparison["bootstrap"][measure]["ci"][0] > 0
    assert [row["year"] for row in comparison["by_year"]] == [2024, 2025]
    assert all(row["filter_mean_pnl"] > row["control_mean_pnl"]
               for row in comparison["by_year"])
    assert comparison["verdict"] == "positive"
    assert "beats its IV-Matched Control" in planted_report["verdict"]
    assert "positive_gamma" in planted_report["verdict"]


def test_regression_reports_its_covariates(planted_report):
    regression = _comparison(planted_report, "positive_gamma")["regression"]
    assert set(regression["coefficients"]) == {"intercept", "filter", "vix",
                                               "atm_iv", "vrp"}
    (row,) = [r for r in planted_report["covariates"] if r["session"] == "2024-03-04"]
    assert row["atm_iv"] == pytest.approx(IV)
    assert row["vix"] is not None and row["vrp"] is not None


def test_verdict_leads_the_json_and_the_summary(tmp_path, capsys):
    _, path = _positive_then_negative_store(tmp_path)
    out, summary = tmp_path / "gex.json", tmp_path / "gex.md"
    assert bt.main(["--store", str(path), "--gex-filter", "--out", str(out),
                    "--summary", str(summary)]) == 0
    report = json.loads(out.read_text())
    assert list(report)[0] == "verdict"
    assert report["verdict"].startswith("VERDICT: ")
    assert summary.read_text().splitlines()[0] == report["verdict"]


def test_cli_summary_needs_the_gex_filter(tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        bt.main(["--store", str(tmp_path / "chain.sqlite3"),
                 "--summary", str(tmp_path / "gex.md")])
    assert raised.value.code == 2
