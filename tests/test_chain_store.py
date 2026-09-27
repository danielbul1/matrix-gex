"""Tests for the chain store schema (tripity_experiment.chain_store)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
from tripity_experiment import chain_store as cs


def _row(strike, snapshot_ms, bid=1.0, **overrides):
    fields = dict(root="SPXW", expiry="2026-03-09", strike=strike, right="P",
                  snapshot_ms=snapshot_ms, bid=bid, ask=bid + 0.1, iv=0.15,
                  sod_oi=1200.0, vendor_delta=-0.16, vendor_gamma=0.002,
                  underlying=5000.0)
    return cs.ChainRow(**{**fields, **overrides})


def test_rows_round_trip_and_filter(tmp_path):
    connection = cs.connect(tmp_path / "chain.sqlite3")
    rows = [_row(4900.0, 1_000), _row(4875.0, 1_000, vendor_delta=None),
            _row(4900.0, 2_000), _row(4900.0, 1_000, root="SPX")]
    assert cs.write_rows(connection, rows) == 4

    assert cs.read_rows(connection) == sorted(
        rows, key=lambda r: (r.snapshot_ms, r.expiry, r.right, r.strike))
    assert cs.read_rows(connection, root="SPXW", end_ms=1_000) == [
        _row(4875.0, 1_000, vendor_delta=None), _row(4900.0, 1_000)]
    assert cs.read_rows(connection, start_ms=2_000) == [_row(4900.0, 2_000)]


def test_rewriting_a_contract_snapshot_replaces_it(tmp_path):
    connection = cs.connect(tmp_path / "chain.sqlite3")
    cs.write_rows(connection, [_row(4900.0, 1_000, bid=1.0)])
    cs.write_rows(connection, [_row(4900.0, 1_000, bid=2.0)])
    assert [r.bid for r in cs.read_rows(connection)] == [2.0]


def test_daily_closes_round_trip_per_symbol(tmp_path):
    path = tmp_path / "chain.sqlite3"
    connection = cs.connect(path)
    cs.write_daily_closes(connection, "SPX", {"2026-03-09": 5010.5})
    cs.write_daily_closes(connection, "VIX", {"2026-03-09": 17.2})
    connection.close()

    reopened = cs.connect(path)  # schema creation is idempotent
    assert cs.read_daily_closes(reopened, "SPX") == {"2026-03-09": 5010.5}
    assert cs.read_daily_closes(reopened, "VIX") == {"2026-03-09": 17.2}


def test_importable_by_the_railway_service_without_pandas():
    # The Railway service does not install pandas, numpy or optopsy.
    code = ("import sys\n"
            "for name in ('pandas', 'numpy', 'optopsy'):\n"
            "    sys.modules[name] = None\n"
            "from tripity_experiment import chain_store\n"
            "assert chain_store.ChainRow\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=ROOT / "railway-service" / "src")
    assert result.returncode == 0, result.stderr
