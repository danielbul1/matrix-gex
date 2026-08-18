"""Tests for the Matrix GEX snapshot store: VEX/CHEX totals, schema
migration, and backwards-compatible history payloads."""
import importlib.util
import sqlite3
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
from tripity_experiment import matrix_gex as mg

ET = mg.ET
VALUATION = int(datetime(2026, 8, 17, 12, 0, tzinfo=ET).timestamp() * 1000)


def _install_third_party_stubs():
    """public_company_host imports FastAPI/uvicorn/etc. at module load; those
    are not installed in every dev environment, so stub them (import-only)."""
    class _Any:
        def __init__(self, *a, **k): pass
        def __call__(self, *a, **k): return _Any()
        def __getattr__(self, name): return _Any()
        def __mro_entries__(self, bases): return (object,)

    for name in (
        "uvicorn", "mcp", "lse_data",
        "fastapi", "fastapi.responses",
        "fastmcp", "fastmcp.client", "fastmcp.client.auth",
        "fastmcp.server", "fastmcp.server.providers", "fastmcp.server.providers.openapi",
        "fastmcp.utilities", "fastmcp.utilities.openapi",
    ):
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue
        except (ModuleNotFoundError, ValueError, TypeError):
            pass
        module = types.ModuleType(name)
        module.__getattr__ = lambda attr: _Any
        sys.modules[name] = module


_install_third_party_stubs()
from tripity_experiment import public_company_host as host  # noqa: E402

RECORD = {
    "mult": 100,
    "opts": [
        {"t": "C", "k": 100, "oi": 5000, "iv": 0.2, "g": 0.0, "d": 0.0, "exp": "2026-08-21", "root": "SPY"},
        {"t": "P", "k": 95, "oi": 8000, "iv": 0.25, "g": 0.0, "d": 0.0, "exp": "2026-08-21", "root": "SPY"},
    ],
}
SPOT = 100.0

OLD_POINT_KEYS = {"time", "spot", "totalGex", "totalDex", "flip",
                  "callWall", "putWall", "maxGammaStrike", "regime"}


def _insert_snapshot_row(db_session, minute_ms, snap):
    host._matrix_gex_db_insert([(
        "SPY", db_session, minute_ms, SPOT,
        snap["total_gex"], snap["total_dex"], snap["total_vex"], snap["total_chex"],
        snap["flip"], snap["call_wall"], snap["put_wall"],
        snap["max_gamma_strike"], snap["regime"],
        snap["atm_iv"], snap["term_slope"],
        snap["gex_scale"], snap["vex_scale"], snap["chex_scale"],
        minute_ms,
    )])


def test_snapshot_includes_vex_chex_totals_with_engine_signs():
    snap = host._compute_matrix_gex_snapshot("SPY", RECORD, SPOT, VALUATION)
    rows = mg.aggregate_strikes(RECORD["opts"], SPOT, 100, VALUATION, include_vc=True)
    assert snap["total_vex"] == pytest.approx(sum(r["net_vex"] for r in rows), rel=1e-12)
    assert snap["total_chex"] == pytest.approx(sum(r["net_chex"] for r in rows), rel=1e-12)

    # Sign conventions: calls carry +vanna/+charm, puts the single negation.
    T = mg.years_to_expiry("2026-08-21", "SPY", VALUATION)
    call_only = host._compute_matrix_gex_snapshot("SPY", {"mult": 100, "opts": RECORD["opts"][:1]}, SPOT, VALUATION)
    assert call_only["total_vex"] == pytest.approx(
        mg.vex_value(mg.bs_vanna(SPOT, 100, T, 0.2), 5000, 100, SPOT), rel=1e-9)
    assert call_only["total_chex"] == pytest.approx(
        mg.chex_value(mg.bs_charm(SPOT, 100, T, 0.2), 5000, 100, SPOT), rel=1e-9)
    put_only = host._compute_matrix_gex_snapshot("SPY", {"mult": 100, "opts": RECORD["opts"][1:]}, SPOT, VALUATION)
    assert put_only["total_vex"] == pytest.approx(
        -mg.vex_value(mg.bs_vanna(SPOT, 95, T, 0.25), 8000, 100, SPOT), rel=1e-9)
    assert put_only["total_chex"] == pytest.approx(
        -mg.chex_value(mg.bs_charm(SPOT, 95, T, 0.25), 8000, 100, SPOT), rel=1e-9)


def test_snapshot_includes_replay_fields():
    """atm_iv / term_slope / the three deadzone scales are computed per
    snapshot so history replay can reactivate the engine's VEX leg."""
    snap = host._compute_matrix_gex_snapshot("SPY", RECORD, SPOT, VALUATION)
    rows = mg.aggregate_strikes(RECORD["opts"], SPOT, 100, VALUATION, include_vc=True)
    # Single-expiry chain: ATM is the 100 strike (iv 0.2); front == back, so
    # the slope is exactly flat.
    assert snap["atm_iv"] == pytest.approx(0.2, rel=1e-12)
    assert snap["term_slope"] == pytest.approx(0.0, abs=1e-12)
    assert snap["gex_scale"] == pytest.approx(
        sum(abs(r["call_gex"]) + abs(r["put_gex"]) for r in rows), rel=1e-12)
    assert snap["vex_scale"] == pytest.approx(
        sum(abs(r["call_vex"]) + abs(r["put_vex"]) for r in rows), rel=1e-12)
    assert snap["chex_scale"] == pytest.approx(
        sum(abs(r["call_chex"]) + abs(r["put_chex"]) for r in rows), rel=1e-12)

    # A two-expiry chain with a stressed front end yields backwardation.
    rich_front = {
        "mult": 100,
        "opts": [
            {"t": "C", "k": 100, "oi": 5000, "iv": 0.45, "g": 0.0, "d": 0.0, "exp": "2026-08-21", "root": "SPY"},
            {"t": "C", "k": 100, "oi": 5000, "iv": 0.20, "g": 0.0, "d": 0.0, "exp": "2026-09-18", "root": "SPY"},
        ],
    }
    stressed = host._compute_matrix_gex_snapshot("SPY", rich_front, SPOT, VALUATION)
    assert stressed["atm_iv"] == pytest.approx(0.45, rel=1e-12)
    assert stressed["term_slope"] == pytest.approx(0.25, rel=1e-9)


def test_schema_migration_on_old_shape_db(tmp_path, monkeypatch):
    db = tmp_path / "old.sqlite3"
    session = host._matrix_session_date().isoformat()
    with sqlite3.connect(db) as connection:
        connection.execute("""
            CREATE TABLE matrix_gex_snapshot (
                symbol TEXT NOT NULL, session_date TEXT NOT NULL,
                minute_ms INTEGER NOT NULL, spot REAL NOT NULL,
                total_gex REAL NOT NULL DEFAULT 0, total_dex REAL NOT NULL DEFAULT 0,
                flip REAL, call_wall REAL, put_wall REAL, max_gamma_strike REAL,
                regime TEXT, updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (symbol, minute_ms)
            )
        """)
        connection.execute(
            "INSERT INTO matrix_gex_snapshot VALUES ('SPY', ?, 60000, 100, 5.5, 6.5, 90, 100, 80, 95, 'neutral', 60000)",
            (session,),
        )
    monkeypatch.setenv("TRIPITY_MATRIX_FLOW_DB", str(db))
    host._matrix_flow_db_init()

    with sqlite3.connect(db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(matrix_gex_snapshot)")}
        assert {"total_vex", "total_chex", "atm_iv", "term_slope",
                "gex_scale", "vex_scale", "chex_scale"} <= columns
        row = connection.execute(
            "SELECT total_gex, total_vex, total_chex, atm_iv, term_slope,"
            " gex_scale, vex_scale, chex_scale FROM matrix_gex_snapshot WHERE symbol='SPY'"
        ).fetchone()
    # old row preserved; numeric additions default to 0, vol fields stay NULL
    assert row == (5.5, 0.0, 0.0, None, None, 0.0, 0.0, 0.0)

    # New rows land alongside migrated ones; the payload stays additive.
    snap = host._compute_matrix_gex_snapshot("SPY", RECORD, SPOT, VALUATION)
    _insert_snapshot_row(session, 120000, snap)
    payload = host._matrix_gex_db_payload("SPY", session)
    assert payload is not None and len(payload["points"]) == 2
    old_point, new_point = payload["points"]
    assert old_point["totalVex"] == 0.0 and old_point["totalChex"] == 0.0
    assert old_point["atmIv"] is None and old_point["termSlope"] is None
    assert old_point["gexScale"] == 0.0 and old_point["vexScale"] == 0.0 and old_point["chexScale"] == 0.0
    assert new_point["totalVex"] == pytest.approx(snap["total_vex"], rel=1e-9)
    assert new_point["totalChex"] == pytest.approx(snap["total_chex"], rel=1e-9)
    assert new_point["atmIv"] == pytest.approx(snap["atm_iv"], rel=1e-9)
    assert new_point["termSlope"] == pytest.approx(snap["term_slope"], abs=1e-12)
    assert new_point["gexScale"] == pytest.approx(snap["gex_scale"], rel=1e-9)
    assert new_point["vexScale"] == pytest.approx(snap["vex_scale"], rel=1e-9)
    assert new_point["chexScale"] == pytest.approx(snap["chex_scale"], rel=1e-9)


def test_history_payload_is_backwards_compatible(tmp_path, monkeypatch):
    db = tmp_path / "new.sqlite3"
    monkeypatch.setenv("TRIPITY_MATRIX_FLOW_DB", str(db))
    session = host._matrix_session_date().isoformat()
    snap = host._compute_matrix_gex_snapshot("SPY", RECORD, SPOT, VALUATION)
    _insert_snapshot_row(session, 60000, snap)

    payload = host._matrix_gex_db_payload("SPY", session)
    assert payload["ok"] is True and payload["history_available"] is True
    assert payload["session_date"] == session
    point = payload["points"][0]
    # Additive fields are present with correct values...
    assert point["totalVex"] == pytest.approx(snap["total_vex"], rel=1e-9)
    assert point["totalChex"] == pytest.approx(snap["total_chex"], rel=1e-9)
    assert point["atmIv"] == pytest.approx(snap["atm_iv"], rel=1e-9)
    assert point["termSlope"] == pytest.approx(snap["term_slope"], abs=1e-12)
    assert point["gexScale"] == pytest.approx(snap["gex_scale"], rel=1e-9)
    assert point["vexScale"] == pytest.approx(snap["vex_scale"], rel=1e-9)
    assert point["chexScale"] == pytest.approx(snap["chex_scale"], rel=1e-9)
    # ...while an old consumer reading only the pre-existing keys is unaffected.
    old_view = {key: point[key] for key in OLD_POINT_KEYS}  # KeyError if a key vanished
    assert old_view["totalGex"] == pytest.approx(snap["total_gex"], rel=1e-9)
    assert old_view["totalDex"] == pytest.approx(snap["total_dex"], rel=1e-9)
    for key in ("symbol", "source", "asof", "interval_seconds", "session_date",
                "points", "history_available"):
        assert key in payload
