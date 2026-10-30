"""Tests for the strategy backtester (tools/backtest_options.py).

Every test builds a small synthetic SPY chain store: Black-Scholes quotes at
IV 20% from a chosen spot, +/- 0.05 around the model price, so a 50% fill
crosses 0.025 per leg. The assertions recompute what each trade should earn
from those quotes by hand.
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
    "backtest_options", ROOT / "tools" / "backtest_options.py")
bo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bo)

ET = mg.ET
MONDAY = date(2025, 3, 3)
EXPIRY = MONDAY + timedelta(days=7)
SPOT = 500.0
IV = 0.20
STRIKES = range(440, 561)
HALF = 0.05  # every quote is the model price +/- HALF
FEE = bo.ETF_FEE_PER_CONTRACT


def _ms(day, hour, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


def _price(spot, strike, expiry, stamp, right):
    T = mg.years_to_expiry(expiry.isoformat(), "SPY", stamp)
    return mg.bs_price(spot, strike, T, IV, is_call=right == "C")


def _chain(session, hour=10, minute=0, spot=SPOT, offsets=(4, 7, 9), root="SPY", oi=None):
    """oi: optional (strike, right) -> open interest; default 1000 everywhere."""
    stamp = _ms(session, hour, minute)
    rows = []
    for offset in offsets:
        expiry = session + timedelta(days=offset)
        for strike in STRIKES:
            for right in ("P", "C"):
                price = _price(spot, strike, expiry, stamp, right)
                rows.append(cs.ChainRow(
                    root=root, expiry=expiry.isoformat(), strike=float(strike), right=right,
                    snapshot_ms=stamp, bid=max(price - HALF, 0.0), ask=price + HALF, iv=IV,
                    sod_oi=1000.0 if oi is None else oi(strike, right), vendor_delta=None, vendor_gamma=None, underlying=spot))
    return rows


def _later(session, day, hour, spot, expiry=None):
    """A snapshot on `day` of the contracts expiring `expiry` (default: the
    7DTE expiry of `session`), at a new spot."""
    expiry = expiry or session + timedelta(days=7)
    return _chain(day, hour, 0, spot=spot, offsets=((expiry - day).days,))


def _store(tmp_path, snapshots, closes, dividends=None):
    path = tmp_path / "chain.sqlite3"
    connection = cs.connect(path)
    for rows in snapshots:
        cs.write_rows(connection, rows)
    cs.write_daily_closes(connection, "SPY", {d.isoformat(): v for d, v in closes.items()})
    if dividends:
        cs.write_dividends(connection, "SPY", {d.isoformat(): v for d, v in dividends.items()})
    connection.close()
    return path


def _delta(strike, right, spot=SPOT, session=MONDAY, expiry=EXPIRY):
    T = mg.years_to_expiry(expiry.isoformat(), "SPY", _ms(session, 10))
    return mg.bs_delta(spot, strike, T, IV, is_call=right == "C")


def _nearest(right, target):
    signed = target if right == "C" else -target
    return float(min(STRIKES, key=lambda k: (abs(_delta(k, right) - signed), k)))


def _model(strike, right, spot=SPOT, day=MONDAY, hour=10, expiry=EXPIRY):
    return _price(spot, strike, expiry, _ms(day, hour), right)


def _fill(model, buying):
    """A 50% fill on a quote of model +/- HALF, the bid floored at zero."""
    bid, ask = max(model - HALF, 0.0), model + HALF
    mid, half = (bid + ask) / 2, (ask - bid) / 2
    return mid + half / 2 if buying else mid - half / 2


def _config(**overrides):
    return bo.OptionsConfig(**{"underlying": "SPY", **overrides})


def _commission(premium):
    return 0.65 if premium >= 0.10 else 0.50 if premium >= 0.05 else 0.25


def _costs(premiums, contracts):
    return sum(max(contracts * _commission(p), 1.0) + contracts * FEE for p in premiums)


def _one_session(tmp_path, *extra, settle=SPOT, dividends=None, closes=None):
    closes = {MONDAY: SPOT, EXPIRY: settle, **(closes or {})}
    return _store(tmp_path, [_chain(MONDAY), *extra], closes, dividends)


def _only_trade(report):
    (trade,) = report["trades"]
    return trade


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------
def test_put_credit_spread_sells_the_target_delta_and_buys_the_wing(tmp_path):
    trade = _only_trade(bo.run_experiment(_one_session(tmp_path), _config(width=5.0)))
    short = _nearest("P", 0.16)
    assert [(leg["side"], leg["right"], leg["strike"]) for leg in trade["legs"]] == [
        ("short", "P", short), ("long", "P", short - 5)]
    assert trade["expiry"] == EXPIRY.isoformat()


def test_iron_condor_adds_the_mirror_call_spread(tmp_path):
    trade = _only_trade(bo.run_experiment(
        _one_session(tmp_path), _config(strategy=bo.IRON_CONDOR, width=3.0)))
    put, call = _nearest("P", 0.16), _nearest("C", 0.16)
    assert [(leg["side"], leg["right"], leg["strike"]) for leg in trade["legs"]] == [
        ("short", "P", put), ("long", "P", put - 3),
        ("short", "C", call), ("long", "C", call + 3)]


@pytest.mark.parametrize("strategy, side", [(bo.SHORT_STRANGLE, "short"),
                                            (bo.LONG_STRANGLE, "long")])
def test_strangles_trade_one_put_and_one_call(tmp_path, strategy, side):
    trade = _only_trade(bo.run_experiment(
        _one_session(tmp_path), _config(strategy=strategy, delta=0.25, width=None)))
    assert [(leg["side"], leg["right"], leg["strike"]) for leg in trade["legs"]] == [
        (side, "P", _nearest("P", 0.25)), (side, "C", _nearest("C", 0.25))]


def test_a_missing_wing_skips_the_session(tmp_path):
    report = bo.run_experiment(_one_session(tmp_path), _config(delta=0.16, width=200.0))
    assert report["trades"] == []
    (skip,) = report["skipped"]
    assert skip["reason"].startswith("no wing strike near")


def test_configurations_reject_a_width_on_the_wrong_strategy():
    with pytest.raises(ValueError, match="takes no width"):
        bo.OptionsConfig(strategy=bo.SHORT_STRANGLE, width=5.0)
    with pytest.raises(ValueError, match="needs width"):
        bo.OptionsConfig(strategy=bo.IRON_CONDOR, width=None)


def test_sessions_are_the_underlyings_own(tmp_path):
    # A day with only QQQ rows is not an SPY session.
    tuesday = MONDAY + timedelta(days=1)
    path = _store(tmp_path, [_chain(MONDAY), _chain(tuesday, root="QQQ")],
                  {MONDAY: SPOT, EXPIRY: SPOT})
    report = bo.run_experiment(path, _config())
    assert report["sessions"] == 1


# ---------------------------------------------------------------------------
# Fills, settlement, costs and sizing
# ---------------------------------------------------------------------------
def test_held_spread_keeps_the_credit_less_costs(tmp_path):
    report = bo.run_experiment(_one_session(tmp_path, settle=520.0), _config(width=5.0))
    trade = _only_trade(report)
    short = _nearest("P", 0.16)
    short_fill = _model(short, "P") - HALF / 2  # sold 50% of the half-spread below mid
    long_fill = _model(short - 5, "P") + HALF / 2
    credit = short_fill - long_fill
    assert trade["entry_cash"] == pytest.approx(credit)

    risk = (5 - credit) * 100
    floor = sum(_commission(p) + FEE for p in (short_fill, long_fill))
    contracts = math.floor(10_000 / (risk + floor))
    assert trade["contracts"] == contracts
    assert trade["exit"] == "expiry"
    assert trade["exit_cash"] == 0.0
    expected = contracts * credit * 100 - _costs((short_fill, long_fill), contracts)
    assert trade["pnl"] == pytest.approx(expected, abs=0.01)


def test_spread_settles_at_intrinsic_below_the_short_strike(tmp_path):
    short = _nearest("P", 0.16)
    trade = _only_trade(bo.run_experiment(
        _one_session(tmp_path, settle=short - 2), _config(width=5.0)))
    assert trade["exit_cash"] == pytest.approx(-2.0)
    # Settlement costs nothing: only the opening order is charged.
    assert trade["costs"] == pytest.approx(
        _costs([leg["fill"] for leg in trade["legs"]], trade["contracts"]), abs=0.01)


def test_short_strangle_risk_is_a_ten_percent_gap(tmp_path):
    trade = _only_trade(bo.run_experiment(
        _one_session(tmp_path), _config(strategy=bo.SHORT_STRANGLE, width=None)))
    put, call = _nearest("P", 0.16), _nearest("C", 0.16)
    credit = trade["entry_cash"]
    gap_loss = max(put - SPOT * 0.9, SPOT * 1.1 - call) - credit
    assert trade["risk_per_position"] == pytest.approx(gap_loss * 100, abs=0.01)


def test_long_strangle_pays_a_debit_and_risks_only_it(tmp_path):
    trade = _only_trade(bo.run_experiment(
        _one_session(tmp_path, settle=540.0),
        _config(strategy=bo.LONG_STRANGLE, delta=0.25, width=None)))
    put, call = _nearest("P", 0.25), _nearest("C", 0.25)
    debit = _model(put, "P") + _model(call, "C") + HALF  # each leg bought HALF/2 over mid
    assert trade["entry_cash"] == pytest.approx(-debit)
    assert trade["risk_per_position"] == pytest.approx(debit * 100, abs=0.01)
    assert trade["exit_cash"] == pytest.approx(540.0 - call)
    assert trade["pnl"] > 0


def test_the_position_cap_skips_the_sixth_open_entry(tmp_path):
    sessions = [MONDAY + timedelta(days=i) for i in (0, 1, 2, 3, 4, 7)]
    closes = {s + timedelta(days=7): SPOT for s in sessions}
    closes.update({s: SPOT for s in sessions})
    report = bo.run_experiment(_store(tmp_path, [_chain(s) for s in sessions], closes),
                               _config(width=5.0))
    assert len(report["trades"]) == 5
    assert report["skipped"] == [{"session": sessions[-1].isoformat(),
                                  "reason": "position cap reached"}]


# ---------------------------------------------------------------------------
# Managed exits
# ---------------------------------------------------------------------------
def test_managed_credit_takes_profit_once_the_buy_back_is_cheap(tmp_path):
    wednesday = MONDAY + timedelta(days=2)
    path = _one_session(tmp_path, _later(MONDAY, wednesday, 11, spot=530.0))
    trade = _only_trade(bo.run_experiment(path, _config(width=5.0, exit_rule="managed")))
    assert trade["exit"] == "take_profit"
    assert trade["exit_time"].startswith(f"{wednesday.isoformat()}T11:00")
    short = _nearest("P", 0.16)
    buy_back = (_fill(_model(short, "P", 530.0, wednesday, 11), buying=True)
                - _fill(_model(short - 5, "P", 530.0, wednesday, 11), buying=False))
    assert trade["exit_cash"] == pytest.approx(-buy_back, abs=1e-6)  # rounded to 6 places


def test_managed_credit_stops_at_twice_the_credit(tmp_path):
    wednesday = MONDAY + timedelta(days=2)
    path = _one_session(tmp_path, _later(MONDAY, wednesday, 11, spot=470.0))
    trade = _only_trade(bo.run_experiment(path, _config(width=5.0, exit_rule="managed")))
    assert trade["exit"] == "stop"
    assert -trade["exit_cash"] >= 2 * trade["entry_cash"]


def test_managed_debit_takes_profit_at_twice_the_debit(tmp_path):
    wednesday = MONDAY + timedelta(days=2)
    path = _one_session(tmp_path, _later(MONDAY, wednesday, 11, spot=530.0))
    trade = _only_trade(bo.run_experiment(path, _config(
        strategy=bo.LONG_STRANGLE, delta=0.25, width=None, exit_rule="managed")))
    assert trade["exit"] == "take_profit"
    assert trade["exit_cash"] >= -2 * trade["entry_cash"]


def test_hold_ignores_intraday_marks(tmp_path):
    wednesday = MONDAY + timedelta(days=2)
    path = _one_session(tmp_path, _later(MONDAY, wednesday, 11, spot=470.0), settle=520.0)
    trade = _only_trade(bo.run_experiment(path, _config(width=5.0)))
    assert trade["exit"] == "expiry"


# ---------------------------------------------------------------------------
# Early assignment
# ---------------------------------------------------------------------------
def _assignment_store(tmp_path, dividend):
    wednesday, thursday = MONDAY + timedelta(days=2), MONDAY + timedelta(days=3)
    return _one_session(
        tmp_path, _later(MONDAY, wednesday, 15, spot=540.0),
        dividends={thursday: dividend}, settle=SPOT,
        closes={MONDAY + timedelta(days=1): 520.0, wednesday: 540.0})


def test_a_short_call_deep_in_the_money_is_assigned_before_the_ex_date(tmp_path):
    wednesday = MONDAY + timedelta(days=2)
    trade = _only_trade(bo.run_experiment(
        _assignment_store(tmp_path, dividend=3.0),
        _config(strategy=bo.SHORT_STRANGLE, width=None)))
    assert trade["exit"] == "assigned"
    assert trade["closed"] == wednesday.isoformat()
    put, call = _nearest("P", 0.16), _nearest("C", 0.16)
    put_buy_back = _fill(_model(put, "P", 540.0, wednesday, 15, EXPIRY), buying=True)
    # The call goes at intrinsic against the close, free; the put is bought back.
    assert trade["exit_cash"] == pytest.approx(-(540.0 - call) - put_buy_back, abs=1e-6)


def test_no_assignment_when_the_dividend_is_below_the_time_value(tmp_path):
    trade = _only_trade(bo.run_experiment(
        _assignment_store(tmp_path, dividend=0.01),
        _config(strategy=bo.SHORT_STRANGLE, width=None)))
    assert trade["exit"] == "expiry"


def test_short_puts_are_never_assigned_early(tmp_path):
    trade = _only_trade(bo.run_experiment(_assignment_store(tmp_path, dividend=3.0),
                                          _config(width=5.0)))
    assert trade["exit"] == "expiry"


# ---------------------------------------------------------------------------
# The Holdout
# ---------------------------------------------------------------------------
def test_the_holdout_is_locked_unless_asked_for(tmp_path):
    late = bo.HOLDOUT_START + timedelta(days=5)  # a Monday
    snapshots = [_chain(MONDAY), _chain(late)]
    closes = {MONDAY: SPOT, EXPIRY: SPOT, late: SPOT, late + timedelta(days=7): SPOT}
    path = _store(tmp_path, snapshots, closes)

    dev = bo.run_experiment(path, _config(width=5.0))
    assert [t["session"] for t in dev["trades"]] == [MONDAY.isoformat()]
    assert dev["period"]["last"] == (bo.HOLDOUT_START - timedelta(days=1)).isoformat()

    # An explicit end inside the Holdout is still cut at its start.
    capped = bo.run_experiment(path, _config(width=5.0), end=late)
    assert [t["session"] for t in capped["trades"]] == [MONDAY.isoformat()]

    holdout = bo.run_experiment(path, _config(width=5.0), holdout=True)
    assert [t["session"] for t in holdout["trades"]] == [late.isoformat()]
    assert holdout["period"]["holdout"] is True


# ---------------------------------------------------------------------------
# The comparison and the command
# ---------------------------------------------------------------------------
def test_compare_runs_every_strategy_beside_buy_and_hold(tmp_path):
    tuesday = MONDAY + timedelta(days=1)
    closes = {MONDAY: SPOT, tuesday: 505.0, EXPIRY: 510.0,
              tuesday + timedelta(days=7): 510.0}
    path = _store(tmp_path, [_chain(MONDAY), _chain(tuesday)], closes)
    report = bo.run_compare(path, ["SPY"])
    block = report["underlyings"]["SPY"]
    assert len(block["rows"]) == report["configurations_tried"] == 38
    assert {row["strategy"] for row in block["rows"]} == set(bo.STRATEGIES)
    assert block["buy_and_hold"]["total_return"] == pytest.approx(505.0 / 500.0 - 1)
    assert "38 configurations were tried" in report["multiple_testing"]

    summary = bo.format_summary(report)
    assert "| iron_condor | 0.16 | 5 | managed |" in summary
    assert "| short_strangle | 0.10 |  | hold |" in summary
    assert "Buy-and-hold" in summary and "Caveats:" in summary


def test_command_writes_the_report_and_summary(tmp_path, capsys):
    path = _one_session(tmp_path)
    out, summary = tmp_path / "compare.json", tmp_path / "compare.md"
    code = bo.main(["--store", str(path), "--compare", "--out", str(out),
                    "--summary", str(summary)])
    capsys.readouterr()
    assert code == 0
    assert json.loads(out.read_text())["configurations_tried"] == 38
    assert summary.read_text().startswith("# Strategy comparison (Dev Period")


def test_command_runs_one_configuration(tmp_path, capsys):
    code = bo.main(["--store", str(_one_session(tmp_path)), "--strategy", "iron_condor",
                    "--width", "3", "--exit-rule", "managed"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["config"]["strategy"] == "iron_condor"
    assert report["config"]["width"] == 3.0
    assert len(report["trades"][0]["legs"]) == 4


def test_command_refuses_a_width_on_a_strangle(tmp_path, capsys):
    code = bo.main(["--store", str(_one_session(tmp_path)), "--strategy",
                    "short_strangle", "--width", "5"])
    assert code == 2
    assert "takes no width" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# GEX at entry, the wall condor and the GEX Filter
# ---------------------------------------------------------------------------
def _walls_oi(put_wall=490, call_wall=510, calls=50_000, puts=50_000):
    """Open interest piled on one call strike and one put strike."""
    def oi(strike, right):
        if right == "C" and strike == call_wall:
            return float(calls)
        if right == "P" and strike == put_wall:
            return float(puts)
        return 100.0
    return oi


def _entry_chain(rows):
    return bo.EntryChain(rows)


def test_gex_finds_the_walls_and_the_sign():
    chain = _entry_chain(_chain(MONDAY, oi=_walls_oi(calls=80_000, puts=20_000)))
    gex = chain.gex()
    assert gex["call_wall"] == 510.0 and gex["put_wall"] == 490.0
    assert gex["net"] > 0  # calls dominate: positive gamma
    flipped = _entry_chain(_chain(MONDAY, oi=_walls_oi(calls=20_000, puts=80_000))).gex()
    assert flipped["net"] < 0


def test_gex_leaves_out_a_dead_same_day_expiry():
    # At a 16:00 entry the 0DTE expiry has no time left; its gamma must not count.
    rows = _chain(MONDAY, 16, offsets=(0, 7), oi=_walls_oi())
    with_zero = _entry_chain(rows).gex()
    without = _entry_chain([r for r in rows if r.expiry != MONDAY.isoformat()]).gex()
    assert with_zero["net"] == pytest.approx(without["net"])


def test_gex_needs_iv_on_most_of_the_open_interest():
    rows = [cs.ChainRow(**{**r.__dict__, "iv": None}) for r in _chain(MONDAY)]
    assert _entry_chain(rows).gex() is None


def test_wall_condor_sells_the_walls(tmp_path):
    path = _store(tmp_path, [_chain(MONDAY, oi=_walls_oi())], {MONDAY: SPOT, EXPIRY: SPOT})
    trade = _only_trade(bo.run_experiment(path, _config(strategy=bo.GEX_WALL_CONDOR,
                                                        delta=None, width=5.0)))
    assert [(leg["side"], leg["right"], leg["strike"]) for leg in trade["legs"]] == [
        ("short", "P", 490.0), ("long", "P", 485.0), ("short", "C", 510.0), ("long", "C", 515.0)]


def test_wall_condor_skips_walls_that_do_not_bracket_spot(tmp_path):
    path = _store(tmp_path, [_chain(MONDAY, oi=_walls_oi(put_wall=505, call_wall=520))],
                  {MONDAY: SPOT, EXPIRY: SPOT})
    report = bo.run_experiment(path, _config(strategy=bo.GEX_WALL_CONDOR, delta=None,
                                             width=5.0))
    assert report["trades"] == []
    assert report["skipped"][0]["reason"] == "GEX walls do not bracket spot"


def test_wall_condor_takes_no_delta():
    with pytest.raises(ValueError, match="takes no delta"):
        bo.OptionsConfig(strategy=bo.GEX_WALL_CONDOR, delta=0.16, width=5.0)


def test_an_end_of_day_store_trades_at_the_close(tmp_path):
    path = _store(tmp_path, [_chain(MONDAY, 16)], {MONDAY: SPOT, EXPIRY: SPOT})
    assert bo.run_experiment(path, _config())["trades"] == []  # nothing by 10:00
    report = bo.run_experiment(path, _config(), entry_time=bo.EOD_ENTRY_TIME)
    assert len(report["trades"]) == 1
    assert report["period"]["entry_time"] == "16:00"


def _filter_store(tmp_path, days=8):
    """Sessions alternating positive and negative gamma, VIX known before each."""
    sessions, day = [], MONDAY
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    snapshots, closes, vix = [], {}, {}
    for i, session in enumerate(sessions):
        heavy = _walls_oi(calls=80_000, puts=20_000) if i % 2 == 0 else _walls_oi(
            calls=20_000, puts=80_000)
        snapshots.append(_chain(session, oi=heavy))
        closes[session] = SPOT
        closes[session + timedelta(days=7)] = SPOT + (5 if i % 2 == 0 else -40)
        vix[session - timedelta(days=1)] = 15.0 + i
    path = _store(tmp_path, snapshots, closes)
    connection = cs.connect(path)
    cs.write_daily_closes(connection, "VIX", {d.isoformat(): v for d, v in vix.items()})
    connection.close()
    return path, sessions


def test_gex_filter_splits_sessions_by_the_gamma_regime(tmp_path):
    path, sessions = _filter_store(tmp_path)
    result = bo.run_gex_filter(path, [_config(width=5.0)])
    (report,) = result["reports"]
    regime = next(f for f in report["filters"] if f["filter"] == bo.FILTER_POSITIVE_GAMMA)
    rows = {row["sessions"]: row for row in regime["rows"]}
    positive = {s.isoformat() for i, s in enumerate(sessions) if i % 2 == 0}
    assert {t["session"] for t in rows[bo.ROW_KEPT]["trades"]} <= positive
    assert rows[bo.ROW_KEPT]["session_count"] + rows[bo.ROW_REJECTED]["session_count"] == \
        rows[bo.ROW_BASELINE]["session_count"] == len(sessions)
    # The control holds as many sessions as the filter keeps: the lowest-VIX ones.
    assert rows[bo.ROW_IV_MATCHED]["session_count"] == rows[bo.ROW_KEPT]["session_count"]
    assert regime["comparison"]["verdict"] in ("positive", "null")
    assert report["verdict"].startswith("VERDICT")
    assert len(result["gex"]) == len(sessions)


def test_gex_filter_needs_a_vix_close_to_decide(tmp_path):
    path = _store(tmp_path, [_chain(MONDAY, oi=_walls_oi())], {MONDAY: SPOT, EXPIRY: SPOT})
    (report,) = bo.run_gex_filter(path, [_config(width=5.0)])["reports"]
    regime = report["filters"][0]
    assert regime["undecided"] == [{"session": MONDAY.isoformat(),
                                    "reason": "no VIX close before the session"}]


def test_gex_study_covers_every_strategy_and_writes_markdown(tmp_path, capsys):
    path, _ = _filter_store(tmp_path, days=4)
    summary = tmp_path / "gex.md"
    code = bo.main(["--store", str(path), "--gex-study", "--summary", str(summary)])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    strategies = [r["config"]["strategy"] for r in report["underlyings"]["SPY"]["reports"]]
    assert strategies == list(bo.STRATEGIES)
    text = summary.read_text()
    assert text.startswith("# GEX Study (Dev Period, entry 10:00 ET)")
    assert "| gex_wall_condor | positive_gamma |" in text


def test_a_quote_above_its_no_arbitrage_bound_is_never_an_exit(tmp_path):
    # The free EOD archive holds placeholder asks near 100,000 on a few SPY days.
    wednesday = MONDAY + timedelta(days=2)
    junk = [cs.ChainRow(**{**r.__dict__, "bid": 1246.0, "ask": 100_003.0})
            for r in _later(MONDAY, wednesday, 11, spot=SPOT)]
    path = _one_session(tmp_path, junk, settle=520.0)
    trade = _only_trade(bo.run_experiment(path, _config(width=5.0, exit_rule="managed")))
    assert trade["exit"] == "expiry"  # the junk marks were skipped
    assert trade["exit_cash"] == 0.0


def test_a_quote_above_its_bound_is_never_an_entry(tmp_path):
    rows = [cs.ChainRow(**{**r.__dict__, "ask": 100_003.0}) if r.right == "P" else r
            for r in _chain(MONDAY)]
    path = _store(tmp_path, [rows], {MONDAY: SPOT, EXPIRY: SPOT})
    report = bo.run_experiment(path, _config(width=5.0))
    assert report["trades"] == []
    assert report["skipped"][0]["reason"] == "missing or crossed quote on a leg"
