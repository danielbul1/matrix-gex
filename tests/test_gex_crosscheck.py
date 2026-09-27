"""Tests for the SqueezeMetrics cross-check (tools/gex_crosscheck.py).

Each test builds a small chain store whose daily Naive GEX is known up to
scale, writes a SqueezeMetrics-format CSV beside it, and asserts on the
correlation and sign agreement the command prints.
"""
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
from tripity_experiment import chain_store as cs
from tripity_experiment import matrix_gex as mg

spec = importlib.util.spec_from_file_location(
    "gex_crosscheck", ROOT / "tools" / "gex_crosscheck.py")
xc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xc)

MONDAY = date(2026, 3, 2)
DAYS = [MONDAY + timedelta(days=i) for i in range(3)]


def _row(day, right, oi):
    stamp = int(datetime(day.year, day.month, day.day, 10, 0,
                         tzinfo=mg.ET).timestamp() * 1000)
    return cs.ChainRow(root="SPXW", expiry=(day + timedelta(days=7)).isoformat(),
                       strike=5000.0, right=right, snapshot_ms=stamp, bid=1.0,
                       ask=1.1, iv=0.15, sod_oi=float(oi), vendor_delta=None,
                       vendor_gamma=None, underlying=5000.0)


def _store(tmp_path):
    """Naive GEX proportional to +1, -2, +3: one ATM contract a day, same
    gamma for calls and puts."""
    path = tmp_path / "chain.sqlite3"
    connection = cs.connect(path)
    cs.write_rows(connection, [_row(DAYS[0], "C", 1000), _row(DAYS[1], "P", 2000),
                               _row(DAYS[2], "C", 3000)])
    connection.close()
    return path


def _csv(tmp_path, gex_by_day):
    path = tmp_path / "DIX.csv"
    lines = ["date,price,dix,gex"] + [
        f"{day.isoformat()},5000.0,0.4,{gex}" for day, gex in gex_by_day.items()]
    path.write_text("\n".join(lines) + "\n")
    return path


def _check(tmp_path, capsys, gex_by_day):
    code = xc.main(["--store", str(_store(tmp_path)),
                    "--csv", str(_csv(tmp_path, gex_by_day))])
    return code, json.loads(capsys.readouterr().out)


def test_matching_series_correlate_and_agree_on_sign(tmp_path, capsys):
    code, report = _check(tmp_path, capsys, {
        DAYS[0]: 1e9, DAYS[1]: -2e9, DAYS[2]: 3e9,
        MONDAY - timedelta(days=1): 5e9})  # not in the store: ignored
    assert code == 0
    assert report["days"] == 3
    assert report["correlation"] == pytest.approx(1.0)
    assert report["sign_agreement"] == pytest.approx(1.0)


def test_a_flipped_sign_shows_in_both_numbers(tmp_path, capsys):
    code, report = _check(tmp_path, capsys, {DAYS[0]: 1e9, DAYS[1]: 2e9, DAYS[2]: 3e9})
    assert code == 0
    assert report["sign_agreement"] == pytest.approx(2 / 3)
    # corr((1, -2, 3), (1, 2, 3)), worked by hand: 2 / sqrt(2 * 114 / 9).
    assert report["correlation"] == pytest.approx(0.3974, abs=1e-4)


def test_too_little_overlap_is_an_error(tmp_path, capsys):
    code, report = _check(tmp_path, capsys, {DAYS[0]: 1e9})
    assert code == 1
    assert report["days"] == 1
    assert report["correlation"] is None
