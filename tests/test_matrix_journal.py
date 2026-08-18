"""Tests for the regime journal (matrix_journal_entries + /api/matrix/journal*).

Covers: schema migration on a legacy DB, entry save/replace/lock rules,
grading correctness with crafted OHLC (user right/wrong, engine right/wrong,
MIXED unscored), stats math (agree/disagree splits, denominators), auto-grade
with a stubbed price provider, and the additive HTTP API shapes end-to-end
through the ASGI app with stubbed host deps.
"""
import asyncio
import importlib.util
import json
import sqlite3
import sys
import types
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
from tripity_experiment import matrix_outcome  # noqa: F401  (rules under test via host)

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 8, 19)       # Wednesday
YESTERDAY = date(2026, 8, 18)   # Tuesday


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


def _entry(day=YESTERDAY.isoformat(), symbol="SPY", user_label="GRIND_UP",
           engine_label="TRAP_DOOR", engine_agreement=2,
           engine_reasoning=("engine sentence",), call_wall=120.0, put_wall=80.0,
           created_ms=1_700_000_000_000):
    return {
        "date": day, "symbol": symbol, "created_ms": created_ms,
        "user_label": user_label, "user_levels": "120 wall", "user_notes": "note",
        "engine_label": engine_label, "engine_agreement": engine_agreement,
        "engine_reasoning": list(engine_reasoning),
        "call_wall": call_wall, "put_wall": put_wall,
    }


@pytest.fixture()
def journal_db(tmp_path, monkeypatch):
    """An isolated flow DB plus a frozen clock: 'today' is TODAY; any earlier
    session is gradeable, TODAY and the future are not."""
    monkeypatch.setenv("TRIPITY_MATRIX_FLOW_DB", str(tmp_path / "matrix_flow.sqlite3"))
    monkeypatch.setattr(host, "_matrix_journal_session_date", lambda now_et=None: TODAY)
    monkeypatch.setattr(
        host, "_matrix_journal_gradeable",
        lambda session_date, now_et=None: date.fromisoformat(session_date) < TODAY)
    return tmp_path / "matrix_flow.sqlite3"


# ---------------------------------------------------------------------------
# Session-date helpers
# ---------------------------------------------------------------------------
def test_session_date_rolls_after_close_and_weekends():
    at = lambda d, h, m: datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
    monday = date(2026, 8, 17)
    assert host._matrix_journal_session_date(at(monday, 10, 0)) == monday
    assert host._matrix_journal_session_date(at(monday, 16, 30)) == date(2026, 8, 18)
    friday = date(2026, 8, 14)
    assert host._matrix_journal_session_date(at(friday, 17, 0)) == monday
    assert host._matrix_journal_session_date(at(date(2026, 8, 15), 11, 0)) == monday  # Saturday


def test_gradeable_only_after_the_close():
    at = lambda h, m: datetime(2026, 8, 19, h, m, tzinfo=ET)
    assert host._matrix_journal_gradeable("2026-08-18", at(10, 0)) is True
    assert host._matrix_journal_gradeable("2026-08-19", at(10, 0)) is False
    assert host._matrix_journal_gradeable("2026-08-19", at(16, 30)) is True
    assert host._matrix_journal_gradeable("2026-08-20", at(16, 30)) is False


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------
def test_migration_on_legacy_db(tmp_path, monkeypatch):
    db = tmp_path / "old.sqlite3"
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
    monkeypatch.setenv("TRIPITY_MATRIX_FLOW_DB", str(db))
    host._matrix_flow_db_init()
    with sqlite3.connect(db) as connection:
        journal_cols = {row[1] for row in connection.execute("PRAGMA table_info(matrix_journal_entries)")}
        snapshot_cols = {row[1] for row in connection.execute("PRAGMA table_info(matrix_gex_snapshot)")}
    assert {
        "session_date", "symbol", "created_ms", "user_label", "user_levels",
        "user_notes", "engine_label", "engine_agreement", "engine_reasoning",
        "call_wall", "put_wall", "outcome", "grade", "engine_grade", "graded_ms",
    } <= journal_cols
    assert {"atm_iv", "term_slope", "gex_scale", "vex_scale", "chex_scale"} <= snapshot_cols


# ---------------------------------------------------------------------------
# Save / replace / lock rules (DB layer)
# ---------------------------------------------------------------------------
def test_save_get_and_replace_while_ungraded(journal_db):
    saved = host._matrix_journal_db_upsert(_entry())
    assert saved["date"] == YESTERDAY.isoformat()
    assert saved["user_label"] == "GRIND_UP"
    assert saved["user_levels"] == "120 wall"
    assert saved["user_notes"] == "note"
    assert saved["engine_label"] == "TRAP_DOOR"
    assert saved["engine_agreement"] == 2
    assert saved["engine_reasoning"] == ["engine sentence"]
    assert saved["call_wall"] == 120.0 and saved["put_wall"] == 80.0
    assert saved["grade"] is None and saved["locked"] is False

    replaced = host._matrix_journal_db_upsert(_entry(user_label="PIN", created_ms=1_700_000_100_000))
    assert replaced["user_label"] == "PIN"
    assert replaced["created_ms"] == 1_700_000_100_000
    assert len(host._matrix_journal_db_list(days=5)) == 1  # one per (date, symbol)


def test_grading_is_write_once_and_survives_upsert(journal_db):
    host._matrix_journal_db_upsert(_entry())
    graded = host._matrix_journal_db_mark_graded(
        YESTERDAY.isoformat(), "SPY", "TREND_UP", 1, 0, 1_700_100_000_000)
    assert graded["locked"] is True and graded["grade"] == 1 and graded["engine_grade"] == 0
    # A second grading attempt is a no-op (returns None, keeps the first grade).
    again = host._matrix_journal_db_mark_graded(
        YESTERDAY.isoformat(), "SPY", "RANGE", 0, 0, 1_700_200_000_000)
    assert again is None
    current = host._matrix_journal_db_get(YESTERDAY.isoformat(), "SPY")
    assert current["outcome"] == "TREND_UP" and current["grade"] == 1
    # The upsert never touches grade fields (the endpoint rejects locked
    # entries before it gets here; the DB layer is defensive regardless).
    host._matrix_journal_db_upsert(_entry(user_label="PIN"))
    current = host._matrix_journal_db_get(YESTERDAY.isoformat(), "SPY")
    assert current["grade"] == 1 and current["outcome"] == "TREND_UP"


# ---------------------------------------------------------------------------
# Grading correctness (crafted OHLC, shared matrix_outcome rules)
# ---------------------------------------------------------------------------
def test_grade_user_right_engine_wrong():
    # drift +2% over a 3.0 range -> efficiency 0.67 -> TREND_UP
    ohlc = (100.0, 102.5, 99.5, 102.0)
    fields = host._matrix_journal_grade_entry(_entry(), ohlc, graded_ms=123)
    assert fields["outcome"] == "TREND_UP"
    assert fields["grade"] == 1          # user said GRIND_UP -> hit
    assert fields["engine_grade"] == 0   # engine said TRAP_DOOR -> miss
    assert fields["graded_ms"] == 123


def test_grade_user_wrong_engine_right():
    ohlc = (100.0, 100.5, 96.8, 97.0)   # efficient -3% drift -> TREND_DOWN
    fields = host._matrix_journal_grade_entry(_entry(), ohlc, graded_ms=1)
    assert fields["outcome"] == "TREND_DOWN"
    assert fields["grade"] == 0          # GRIND_UP does not pay on a down trend
    assert fields["engine_grade"] == 1   # TRAP_DOOR does


def test_grade_pin_outcome_uses_frozen_walls():
    entry = _entry(user_label="PIN", engine_label="GRIND_UP", call_wall=101.0, put_wall=90.0)
    ohlc = (100.0, 101.2, 99.8, 101.1)  # close glued to the 101 wall
    fields = host._matrix_journal_grade_entry(entry, ohlc, graded_ms=1)
    assert fields["outcome"] == "PIN"
    assert fields["grade"] == 1          # PIN pays on PIN
    assert fields["engine_grade"] == 0   # GRIND_UP does not


def test_grade_mixed_is_unscored():
    ohlc = (100.0, 102.5, 99.5, 102.0)
    fields = host._matrix_journal_grade_entry(
        _entry(user_label="MIXED", engine_label="MIXED"), ohlc, graded_ms=1)
    assert fields["outcome"] == "TREND_UP"
    assert fields["grade"] is None         # MIXED makes no prediction
    assert fields["engine_grade"] is None
    assert fields["graded_ms"] == 1        # ...but the day is still marked graded


def test_grade_without_prices_returns_none():
    assert host._matrix_journal_grade_entry(_entry(), None) is None


def test_candles_ohlc_picks_the_requested_session():
    def bar(day, hour, o, h, l, c):
        stamp = datetime(day.year, day.month, day.day, hour, 0, tzinfo=ET).timestamp()
        return {"time": stamp, "open": o, "high": h, "low": l, "close": c}
    candles = [
        bar(YESTERDAY, 9, 100.0, 100.5, 99.5, 100.2),
        bar(YESTERDAY, 15, 100.2, 102.0, 100.0, 101.8),
        bar(TODAY, 9, 101.8, 102.2, 101.0, 101.5),
    ]
    ohlc = host._matrix_journal_candles_ohlc(candles, YESTERDAY.isoformat())
    assert ohlc == (100.0, 102.0, 99.5, 101.8)
    assert host._matrix_journal_candles_ohlc(candles, "2026-08-17") is None


# ---------------------------------------------------------------------------
# Stats math (agree/disagree splits, denominators)
# ---------------------------------------------------------------------------
def _scored(day, user_label, grade, engine_label, engine_grade):
    return {
        "date": day, "symbol": "SPY", "created_ms": 1,
        "user_label": user_label, "user_levels": "", "user_notes": "",
        "engine_label": engine_label, "engine_agreement": 1, "engine_reasoning": [],
        "call_wall": None, "put_wall": None, "outcome": "TREND_UP",
        "grade": grade, "engine_grade": engine_grade, "graded_ms": 2, "locked": True,
    }


def test_stats_math(journal_db):
    entries = [
        _scored("2026-08-11", "GRIND_UP", 1, "GRIND_UP", 1),   # agree, both hit
        _scored("2026-08-12", "TRAP_DOOR", 0, "PIN", 1),       # disagree, user wrong
        _scored("2026-08-13", "PIN", 1, "PIN", 0),             # agree, user right engine wrong
        _scored("2026-08-14", "MIXED", None, "TRAP_DOOR", 0),  # user unscored
        {**_scored("2026-08-17", "PIN", 1, "PIN", 1), "graded_ms": None, "locked": False,
         "grade": None, "engine_grade": None, "outcome": None},  # ungraded: excluded
    ]
    stats = host._matrix_journal_stats(entries)
    assert stats["ok"] is True
    assert stats["entries"] == 5 and stats["graded"] == 4
    # user: MIXED excluded from the denominator
    assert stats["user"] == {"scored": 3, "hits": 2, "hit_rate": pytest.approx(2 / 3)}
    # engine is scored on every graded day, incl. the user's MIXED day
    assert stats["engine"] == {"scored": 4, "hits": 2, "hit_rate": pytest.approx(0.5)}
    # agree/disagree split (only days with both a user score and an engine label)
    assert stats["user_when_agreeing"] == {"scored": 2, "hits": 2, "hit_rate": pytest.approx(1.0)}
    assert stats["user_when_disagreeing"] == {"scored": 1, "hits": 0, "hit_rate": pytest.approx(0.0)}
    # per-label breakdown
    assert stats["by_label"]["user"]["GRIND_UP"] == {"scored": 1, "hits": 1, "hit_rate": pytest.approx(1.0)}
    assert stats["by_label"]["user"]["TRAP_DOOR"] == {"scored": 1, "hits": 0, "hit_rate": pytest.approx(0.0)}
    assert stats["by_label"]["user"]["MIXED"] == {"scored": 0, "hits": 0, "hit_rate": None}
    assert stats["by_label"]["engine"]["PIN"] == {"scored": 2, "hits": 1, "hit_rate": pytest.approx(0.5)}
    # small-sample caution fires well below 30 scored days
    assert stats["small_sample"] is True
    assert stats["small_sample_threshold"] == 30
    assert stats["caution"]


def test_stats_empty_journal(journal_db):
    stats = host._matrix_journal_stats([])
    assert stats["user"] == {"scored": 0, "hits": 0, "hit_rate": None}
    assert stats["engine"] == {"scored": 0, "hits": 0, "hit_rate": None}
    assert stats["graded"] == 0 and stats["small_sample"] is True


# ---------------------------------------------------------------------------
# Auto-grade with a stubbed price provider
# ---------------------------------------------------------------------------
def test_auto_grade_grades_only_completed_sessions(journal_db):
    host._matrix_journal_db_upsert(_entry(day=YESTERDAY.isoformat()))  # gradeable
    host._matrix_journal_db_upsert(_entry(day=TODAY.isoformat()))      # still open

    async def provider(symbol, session_date):
        assert symbol == "SPY"
        return (100.0, 102.5, 99.5, 102.0)  # TREND_UP

    graded = asyncio.run(host._matrix_journal_auto_grade(provider=provider))
    assert [g["date"] for g in graded] == [YESTERDAY.isoformat()]
    past = host._matrix_journal_db_get(YESTERDAY.isoformat(), "SPY")
    assert past["outcome"] == "TREND_UP" and past["grade"] == 1 and past["engine_grade"] == 0
    today = host._matrix_journal_db_get(TODAY.isoformat(), "SPY")
    assert today["locked"] is False and today["outcome"] is None

    # Idempotent: nothing left to grade.
    assert asyncio.run(host._matrix_journal_auto_grade(provider=provider)) == []


def test_auto_grade_skips_days_without_prices(journal_db):
    host._matrix_journal_db_upsert(_entry())

    async def provider(symbol, session_date):
        return None

    assert asyncio.run(host._matrix_journal_auto_grade(provider=provider)) == []
    assert host._matrix_journal_db_get(YESTERDAY.isoformat(), "SPY")["locked"] is False


# ---------------------------------------------------------------------------
# HTTP API (ASGI app with stubbed host deps)
# ---------------------------------------------------------------------------
def _make_app():
    connector = types.SimpleNamespace(
        slug="t", company_name="t", openapi_url="", api_base_url="",
        mcp_path="/mcp/t", enabled_tools=(), disabled_tools=(), app=None)
    return host.PublicCompanyApp(connector, oauth=False, public_url="http://test")


def _request(app, method, path, query="", body=None):
    scope = {"type": "http", "method": method, "path": path,
             "query_string": query.encode(), "headers": []}
    bodies = [json.dumps(body or {}).encode()]
    sent = []

    async def receive():
        chunk = bodies.pop(0) if bodies else b""
        return {"type": "http.request", "body": chunk, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(payload.decode()) if payload else {}


@pytest.fixture()
def stubbed_engine(monkeypatch):
    async def fake_regime(symbol):
        return (
            {"label": "PIN", "agreement": 2, "direction": 1,
             "reasoning": ["gex sentence", "vex sentence"]},
            {"call_wall": 101.0, "put_wall": 95.0},
        )
    monkeypatch.setattr(host, "_compute_matrix_regime", fake_regime)


@pytest.fixture()
def stubbed_ohlc(monkeypatch):
    async def fake_day_ohlc(symbol, session_date):
        return (100.0, 102.5, 99.5, 102.0)  # TREND_UP
    monkeypatch.setattr(host, "_matrix_journal_day_ohlc", fake_day_ohlc)


def test_post_journal_saves_and_freezes_engine(journal_db, stubbed_engine):
    app = _make_app()
    status, payload = _request(app, "POST", "/api/matrix/journal", body={
        "symbol": "SPY", "user_label": "grind_up",
        "user_levels": "101 wall", "notes": "opex week"})
    assert status == 201
    assert payload["ok"] is True and payload["engine_frozen"] is True
    entry = payload["entry"]
    assert entry["date"] == TODAY.isoformat()      # frozen session date
    assert entry["user_label"] == "GRIND_UP"       # normalized
    assert entry["user_levels"] == "101 wall" and entry["user_notes"] == "opex week"
    assert entry["engine_label"] == "PIN"          # engine verdict frozen
    assert entry["engine_agreement"] == 2
    assert entry["engine_reasoning"] == ["gex sentence", "vex sentence"]
    assert entry["call_wall"] == 101.0 and entry["put_wall"] == 95.0
    assert entry["locked"] is False and entry["grade"] is None

    # Replace while ungraded.
    status, payload = _request(app, "POST", "/api/matrix/journal", body={
        "symbol": "SPY", "user_label": "PIN"})
    assert status == 201 and payload["entry"]["user_label"] == "PIN"


def test_post_journal_rejects_bad_input_and_locked_entries(journal_db, stubbed_engine):
    app = _make_app()
    status, payload = _request(app, "POST", "/api/matrix/journal", body={
        "symbol": "SPY", "user_label": "MOON"})
    assert status == 400 and payload["ok"] is False
    status, payload = _request(app, "POST", "/api/matrix/journal", body={
        "symbol": "AAPL", "user_label": "PIN"})
    assert status == 400 and "Unsupported" in payload["detail"]

    status, payload = _request(app, "POST", "/api/matrix/journal", body={
        "symbol": "SPY", "user_label": "PIN"})
    assert status == 201
    host._matrix_journal_db_mark_graded(TODAY.isoformat(), "SPY", "PIN", 1, 1, 999)
    status, payload = _request(app, "POST", "/api/matrix/journal", body={
        "symbol": "SPY", "user_label": "GRIND_UP"})
    assert status == 409
    assert payload["ok"] is False and "locked" in payload["detail"]
    assert payload["entry"]["user_label"] == "PIN"  # the locked call is returned


def test_post_journal_survives_engine_failure(journal_db, monkeypatch):
    async def broken(symbol):
        raise RuntimeError("market data down")
    monkeypatch.setattr(host, "_compute_matrix_regime", broken)
    app = _make_app()
    status, payload = _request(app, "POST", "/api/matrix/journal", body={
        "symbol": "SPY", "user_label": "PIN"})
    assert status == 201
    assert payload["engine_frozen"] is False
    assert payload["entry"]["engine_label"] is None


def test_get_journal_auto_grades_past_days(journal_db, stubbed_engine, stubbed_ohlc):
    host._matrix_journal_db_upsert(_entry(day=YESTERDAY.isoformat()))
    host._matrix_journal_db_upsert(_entry(day=TODAY.isoformat(), user_label="PIN"))
    app = _make_app()
    status, payload = _request(app, "GET", "/api/matrix/journal", query="days=60")
    assert status == 200
    assert payload["ok"] is True
    assert payload["session_date"] == TODAY.isoformat()
    assert payload["labels"] == ["GRIND_UP", "TRAP_DOOR", "SQUEEZE", "PIN", "MIXED"]
    assert payload["newly_graded"] == 1
    by_date = {e["date"]: e for e in payload["entries"]}
    past = by_date[YESTERDAY.isoformat()]
    assert past["outcome"] == "TREND_UP" and past["grade"] == 1 and past["engine_grade"] == 0
    assert past["locked"] is True
    today = by_date[TODAY.isoformat()]
    assert today["locked"] is False and today["outcome"] is None


def test_post_journal_grade_endpoint(journal_db, stubbed_ohlc):
    host._matrix_journal_db_upsert(_entry(day=YESTERDAY.isoformat()))
    app = _make_app()
    status, payload = _request(app, "POST", "/api/matrix/journal/grade", body={
        "date": YESTERDAY.isoformat(), "symbol": "SPY"})
    assert status == 200
    assert payload["graded_count"] == 1
    graded = payload["graded"][0]
    assert graded["outcome"] == "TREND_UP" and graded["grade"] == 1 and graded["engine_grade"] == 0

    # Re-grading is a safe no-op that reports the existing grade.
    status, payload = _request(app, "POST", "/api/matrix/journal/grade", body={
        "date": YESTERDAY.isoformat(), "symbol": "SPY"})
    assert status == 200 and payload["detail"] == "already graded"
    assert payload["entry"]["grade"] == 1

    # The open session cannot be graded yet; unknown days 404.
    status, payload = _request(app, "POST", "/api/matrix/journal/grade", body={
        "date": TODAY.isoformat(), "symbol": "SPY"})
    assert status == 400 and "not complete" in payload["detail"]
    status, payload = _request(app, "POST", "/api/matrix/journal/grade", body={
        "date": "2026-08-10", "symbol": "SPY"})
    assert status == 404
    status, payload = _request(app, "POST", "/api/matrix/journal/grade", body={
        "date": "not-a-date"})
    assert status == 400


def test_get_journal_stats_endpoint(journal_db, stubbed_ohlc):
    host._matrix_journal_db_upsert(_entry(day="2026-08-17", user_label="GRIND_UP", engine_label="GRIND_UP"))
    host._matrix_journal_db_upsert(_entry(day="2026-08-18", user_label="TRAP_DOOR", engine_label="PIN"))
    app = _make_app()
    status, stats = _request(app, "GET", "/api/matrix/journal/stats", query="days=60")
    assert status == 200
    assert stats["ok"] is True and stats["days"] == 60
    assert stats["graded"] == 2  # both past days auto-graded on the way out
    # Both days TREND_UP: GRIND_UP hits, TRAP_DOOR misses, PIN misses.
    assert stats["user"] == {"scored": 2, "hits": 1, "hit_rate": pytest.approx(0.5)}
    assert stats["engine"] == {"scored": 2, "hits": 1, "hit_rate": pytest.approx(0.5)}
    assert stats["user_when_agreeing"] == {"scored": 1, "hits": 1, "hit_rate": pytest.approx(1.0)}
    assert stats["user_when_disagreeing"] == {"scored": 1, "hits": 0, "hit_rate": pytest.approx(0.0)}
    assert stats["by_label"]["user"]["GRIND_UP"]["scored"] == 1
    assert stats["small_sample"] is True and stats["caution"]
