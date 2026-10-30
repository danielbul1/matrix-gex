"""Tests for the end-of-day archive import (tools/import_eod_chain.py).

Each test writes a tiny archive in the published layout (one options file,
one underlying file) and asserts on what lands in the chain store.
"""
import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
from tripity_experiment import chain_store as cs
from tripity_experiment import matrix_gex as mg

spec = importlib.util.spec_from_file_location(
    "import_eod_chain", ROOT / "tools" / "import_eod_chain.py")
ie = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ie)

pytest.importorskip("pyarrow")
MONDAY, TUESDAY = date(2024, 3, 4), date(2024, 3, 5)


def _option(day, expiry, strike, kind, oi=100):
    return {"contract_id": f"SPY{expiry:%y%m%d}{kind[0].upper()}{int(strike * 1000):08d}",
            "symbol": "SPY", "expiration": pd.Timestamp(expiry), "strike": float(strike),
            "type": kind, "last": 1.0, "mark": 1.05, "bid": 1.0, "ask": 1.1, "bid_size": 1,
            "ask_size": 1, "volume": 5, "open_interest": oi, "date": pd.Timestamp(day),
            "implied_volatility": 0.18, "delta": 0.3 if kind == "call" else -0.3,
            "gamma": 0.02, "theta": -0.1, "vega": 0.2, "rho": 0.01, "in_the_money": False}


def _archive(tmp_path, options):
    options_path = tmp_path / "SPY_options.parquet"
    underlying_path = tmp_path / "SPY_underlying.parquet"
    pd.DataFrame(options).to_parquet(options_path, row_group_size=2)
    pd.DataFrame({
        "id": [1, 2], "symbol": "SPY", "date": [MONDAY.isoformat(), TUESDAY.isoformat()],
        "open": 500.0, "high": 505.0, "low": 495.0, "close": [500.0, 502.0],
        "adjusted_close": 499.0, "volume": 1, "dividend_amount": [0.0, 1.74],
        "split_coefficient": 1.0, "created_at": "2025-12-15",
    }).to_parquet(underlying_path)
    return options_path, underlying_path


def _rows(path):
    connection = cs.connect(path)
    try:
        return cs.read_rows(connection)
    finally:
        connection.close()


def test_rows_land_as_one_close_snapshot_per_day(tmp_path):
    options, underlying = _archive(tmp_path, [
        _option(MONDAY, date(2024, 3, 8), 500, "call", oi=1234),
        _option(MONDAY, date(2024, 3, 8), 495, "put"),
        _option(TUESDAY, date(2024, 3, 8), 500, "call"),
    ])
    store = tmp_path / "chain.sqlite3"
    summary = ie.import_chain(store, options, underlying, "SPY")
    assert summary == {"symbol": "SPY", "rows_written": 3, "sessions": 2,
                       "first": MONDAY.isoformat(), "last": TUESDAY.isoformat()}
    rows = _rows(store)
    close_ms = int(datetime(2024, 3, 4, 16, 0, tzinfo=mg.ET).timestamp() * 1000)
    call = next(r for r in rows if r.snapshot_ms == close_ms and r.right == "C")
    assert (call.root, call.expiry, call.strike, call.bid, call.ask) == (
        "SPY", "2024-03-08", 500.0, 1.0, 1.1)
    assert call.iv == pytest.approx(0.18)
    assert call.sod_oi == 1234.0 and call.vendor_gamma == pytest.approx(0.02)
    assert call.underlying == 500.0  # the day's close
    assert {r.underlying for r in rows if r.snapshot_ms != close_ms} == {502.0}


def test_far_expiries_and_far_strikes_are_dropped(tmp_path):
    options, underlying = _archive(tmp_path, [
        _option(MONDAY, date(2024, 3, 8), 500, "call"),
        _option(MONDAY, date(2024, 6, 21), 500, "call"),  # 109 days out
        _option(MONDAY, date(2024, 3, 8), 300, "put"),  # 40% below the close
    ])
    store = tmp_path / "chain.sqlite3"
    assert ie.import_chain(store, options, underlying, "SPY")["rows_written"] == 1


def test_closes_and_dividends_are_loaded(tmp_path):
    options, underlying = _archive(tmp_path, [_option(MONDAY, date(2024, 3, 8), 500, "call")])
    store = tmp_path / "chain.sqlite3"
    ie.import_chain(store, options, underlying, "SPY")
    connection = cs.connect(store)
    assert cs.read_daily_closes(connection, "SPY") == {MONDAY.isoformat(): 500.0,
                                                       TUESDAY.isoformat(): 502.0}
    assert cs.read_dividends(connection, "SPY") == {TUESDAY.isoformat(): 1.74}
    connection.close()


def test_start_and_end_bound_the_sessions(tmp_path):
    options, underlying = _archive(tmp_path, [
        _option(MONDAY, date(2024, 3, 8), 500, "call"),
        _option(TUESDAY, date(2024, 3, 8), 500, "call"),
    ])
    store = tmp_path / "chain.sqlite3"
    assert ie.import_chain(store, options, underlying, "SPY", start=TUESDAY)["first"] == \
        TUESDAY.isoformat()
    other = tmp_path / "other.sqlite3"
    assert ie.import_chain(other, options, underlying, "SPY", end=MONDAY)["last"] == \
        MONDAY.isoformat()


def test_command_names_the_symbol_from_the_file_and_loads_vix(tmp_path, capsys):
    options, underlying = _archive(tmp_path, [_option(MONDAY, date(2024, 3, 8), 500, "call")])
    store = tmp_path / "chain.sqlite3"
    csv = "DATE,OPEN,HIGH,LOW,CLOSE\n03/04/2024,14,15,13,14.5\n"
    code = ie.main(["--store", str(store), "--options", str(options),
                    "--underlying", str(underlying), "--vix"],
                   fetch_text=lambda url: csv if "VIX" in url else "DATE,SPX\n")
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["symbol"] == "SPY"
    connection = cs.connect(store)
    assert cs.read_daily_closes(connection, "VIX") == {MONDAY.isoformat(): 14.5}
    connection.close()


def test_verify_flags_a_file_that_differs_from_its_pinned_hash(tmp_path, capsys):
    options, underlying = _archive(tmp_path, [_option(MONDAY, date(2024, 3, 8), 500, "call")])
    assert ie.verify(options) is False  # same name as the release file, other bytes
    assert ie.verify(tmp_path / "unknown.parquet") is None
    code = ie.main(["--store", str(tmp_path / "s.sqlite3"), "--options", str(options),
                    "--underlying", str(underlying), "--verify"])
    assert code == 2
    assert "does not match its pinned hash" in capsys.readouterr().err
