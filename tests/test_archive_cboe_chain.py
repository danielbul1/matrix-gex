"""Tests for the end-of-day Cboe archive (tools/archive_cboe_chain.py) and
its import (tools/import_eod_chain.py --archive)."""
import csv
import datetime
import gzip
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
from tripity_experiment import chain_store as cs

spec = importlib.util.spec_from_file_location(
    "archive_cboe_chain", ROOT / "tools" / "archive_cboe_chain.py")
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)
spec = importlib.util.spec_from_file_location(
    "import_eod_chain", ROOT / "tools" / "import_eod_chain.py")
ie = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ie)

SESSION = datetime.date(2026, 10, 29)
EVENING = datetime.datetime(2026, 10, 29, 16, 40, tzinfo=ac.ET)


def _option(expiry, right, strike, **fields):
    return {"option": f"SPY{expiry:%y%m%d}{right}{int(strike * 1000):08d}", "bid": 1.0,
            "ask": 1.1, "iv": 0.15, "open_interest": 100.0, "volume": 7.0, "delta": 0.3,
            "gamma": 0.02, **fields}


def _chain(spot=700.0, last_trade="2026-10-29T16:00:00", options=None):
    week = SESSION + datetime.timedelta(days=7)
    return {"symbol": "SPY", "current_price": spot, "close": spot,
            "last_trade_time": last_trade,
            "options": options if options is not None else [
                _option(week, "C", 710), _option(week, "P", 690, open_interest=0.0),
                _option(SESSION + datetime.timedelta(days=90), "C", 700),  # too far out
                _option(week, "P", 400),  # outside the strike band
            ]}


def _read(path):
    with gzip.open(path, "rt") as handle:
        return list(csv.DictReader(handle))


def test_the_evening_snapshot_is_written_in_the_archive_columns(tmp_path):
    result = ac.archive(tmp_path, ("SPY",), now=EVENING, fetch=lambda s: _chain())
    path = tmp_path / "SPY" / "2026-10-29.csv.gz"
    assert result == {"SPY": f"wrote 2 rows: {path}"}
    rows = _read(path)
    assert tuple(rows[0]) == ac.COLUMNS
    assert [(r["type"], float(r["strike"]), r["expiration"]) for r in rows] == [
        ("call", 710.0, "2026-11-05"), ("put", 690.0, "2026-11-05")]
    assert rows[1]["open_interest"] == "0.0"  # kept: it still prices a wing
    assert {r["underlying"] for r in rows} == {"700.0"}


@pytest.mark.parametrize("now", [
    datetime.datetime(2026, 10, 29, 15, 50, tzinfo=ac.ET),  # before the options close
    datetime.datetime(2026, 10, 30, 8, 0, tzinfo=ac.ET),  # next morning: new open interest
])
def test_nothing_is_written_outside_the_sessions_evening(tmp_path, now):
    result = ac.archive(tmp_path, ("SPY",), now=now, fetch=lambda s: _chain())
    assert result["SPY"].startswith("skipped: session 2026-10-29")
    assert not list(tmp_path.rglob("*.csv.gz"))


def test_an_archived_session_is_never_rewritten(tmp_path):
    ac.archive(tmp_path, ("SPY",), now=EVENING, fetch=lambda s: _chain())
    path = tmp_path / "SPY" / "2026-10-29.csv.gz"
    before = path.read_bytes()
    result = ac.archive(tmp_path, ("SPY",), now=EVENING, fetch=lambda s: _chain(spot=999.0))
    assert result["SPY"].startswith("exists")
    assert path.read_bytes() == before


def test_one_failing_symbol_does_not_stop_the_others(tmp_path, capsys):
    def fetch(symbol):
        if symbol == "QQQ":
            raise ConnectionError("down")
        return _chain()
    code = ac.main(["--out", str(tmp_path), "--symbols", "SPY,QQQ,_SPX"], fetch=fetch,
                   now=EVENING)
    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result["QQQ"] == "failed: ConnectionError: down"
    assert result["SPY"].startswith("wrote") and result["SPX"].startswith("wrote")
    assert (tmp_path / "SPX" / "2026-10-29.csv.gz").exists()  # no leading underscore


def test_the_archive_imports_into_the_chain_store(tmp_path):
    ac.archive(tmp_path / "archive", ("SPY",), now=EVENING, fetch=lambda s: _chain())
    store = tmp_path / "chain.sqlite3"
    summary = ie.import_archive(store, tmp_path / "archive", "SPY")
    assert summary == {"symbol": "SPY", "rows_written": 2, "sessions": 1,
                       "first": "2026-10-29", "last": "2026-10-29"}
    connection = cs.connect(store)
    rows = cs.read_rows(connection)
    assert {r.underlying for r in rows} == {700.0}
    assert {r.root for r in rows} == {"SPY"}
    assert cs.read_daily_closes(connection, "SPY") == {"2026-10-29": 700.0}
    connection.close()


def test_the_import_command_takes_an_archive(tmp_path, capsys):
    ac.archive(tmp_path / "archive", ("SPY",), now=EVENING, fetch=lambda s: _chain())
    code = ie.main(["--store", str(tmp_path / "s.sqlite3"), "--archive",
                    str(tmp_path / "archive"), "--symbol", "SPY"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["rows_written"] == 2
    with pytest.raises(SystemExit):
        ie.parse_args(["--store", "s", "--archive", "a"])  # --symbol is required
