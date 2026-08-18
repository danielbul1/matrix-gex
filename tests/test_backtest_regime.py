"""Tests for the regime backtester (tools/backtest_regime.py).

Covers: outcome classification rules on crafted OHLC, the label->outcome
prediction mapping (incl. MIXED exclusion), decision-snapshot selection,
scoring math (per label / per agreement / confusion matrix), and an
end-to-end replay over a synthetic multi-day SQLite snapshot store.
"""
import importlib.util
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
from tripity_experiment import matrix_regime as mr

spec = importlib.util.spec_from_file_location(
    "backtest_regime", ROOT / "tools" / "backtest_regime.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

ET = mr.ET
# 2026-08-21 is the August 2026 monthly OpEx (3rd Friday).
OPEX = date(2026, 8, 21)
PIN_DAY = date(2026, 8, 19)    # Wednesday, 2 days before OpEx
PIN_DAY_2 = date(2026, 8, 20)  # Thursday, 1 day before OpEx
FAR_DAY = date(2026, 8, 13)    # 8 days before OpEx -> charm too weak


def _ms(day, hour, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Outcome classifier: crafted OHLC
# ---------------------------------------------------------------------------
def test_classify_trend_up():
    # drift +1.8% over a 2.5 range -> efficiency 0.72 >= 0.5
    outcome, drift, wall = bt.classify_outcome(100, 102, 99.5, 101.8, walls=(140, 60))
    assert outcome == "TREND_UP"
    assert drift == pytest.approx(0.018)
    assert wall is None


def test_classify_trend_down():
    outcome, drift, _ = bt.classify_outcome(100, 100.5, 96.8, 97.0, walls=(140, 60))
    assert outcome == "TREND_DOWN"
    assert drift == pytest.approx(-0.03)


def test_classify_range_when_drift_dissipates():
    # big round trip, close back near the open -> efficiency ~0.05
    outcome, _, _ = bt.classify_outcome(100, 101, 99, 100.1, walls=(140, 60))
    assert outcome == "RANGE"


def test_classify_range_when_drift_too_small_despite_efficiency():
    # efficient but only +0.1% net drift: below TREND_MIN_DRIFT_PCT
    outcome, _, _ = bt.classify_outcome(100, 100.15, 99.95, 100.1, walls=(140, 60))
    assert outcome == "RANGE"


def test_classify_pin_beats_trend():
    # close glued to the 101 wall (0.1% away) even though it drifted up
    outcome, _, wall = bt.classify_outcome(100, 101.2, 99.8, 101.1, walls=(101, 90))
    assert outcome == "PIN"
    assert wall == 101


def test_classify_pin_boundary():
    # exactly OUTCOME_PIN_WALL_PCT away still counts; just outside does not
    close = 100.0
    near = close * (1 + bt.OUTCOME_PIN_WALL_PCT)
    assert bt.classify_outcome(99, near + 0.5, 98.5, close, walls=(near,))[0] == "PIN"
    far = close * (1 + bt.OUTCOME_PIN_WALL_PCT + 0.001)
    assert bt.classify_outcome(99, far + 0.5, 98.5, close, walls=(far,))[0] != "PIN"


def test_classify_zero_range_is_range_not_crash():
    assert bt.classify_outcome(100, 100, 100, 100, walls=(140,))[0] == "RANGE"


def test_classify_rejects_nonpositive_open():
    with pytest.raises(ValueError):
        bt.classify_outcome(0, 1, 1, 1)


# ---------------------------------------------------------------------------
# Prediction mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label,outcome,drift,expected", [
    ("GRIND_UP", "TREND_UP", 0.01, True),
    ("GRIND_UP", "RANGE", 0.001, True),    # upward-drifting chop still pays
    ("GRIND_UP", "RANGE", -0.001, False),  # downward drift does not
    ("GRIND_UP", "TREND_DOWN", -0.01, False),
    ("TRAP_DOOR", "TREND_DOWN", -0.01, True),
    ("TRAP_DOOR", "RANGE", 0.0, False),
    ("TRAP_DOOR", "TREND_UP", 0.01, False),
    ("SQUEEZE", "TREND_UP", 0.02, True),
    ("SQUEEZE", "TREND_DOWN", -0.02, False),
    ("PIN", "PIN", 0.0, True),
    ("PIN", "RANGE", 0.0, True),
    ("PIN", "TREND_UP", 0.01, False),
    ("MIXED", "TREND_UP", 0.01, None),     # no prediction -> excluded
    ("MIXED", "TREND_DOWN", -0.01, None),
])
def test_predicts_mapping(label, outcome, drift, expected):
    assert bt.predicts(label, outcome, drift) is expected


# ---------------------------------------------------------------------------
# Decision snapshot selection
# ---------------------------------------------------------------------------
def test_select_decision_point_picks_latest_at_or_before():
    points = [
        {"minute_ms": _ms(PIN_DAY, 9, 35)},
        {"minute_ms": _ms(PIN_DAY, 9, 59)},
        {"minute_ms": _ms(PIN_DAY, 10, 1)},
    ]
    chosen = bt.select_decision_point(points, _ms(PIN_DAY, 10, 0))
    assert chosen["minute_ms"] == _ms(PIN_DAY, 9, 59)


def test_select_decision_point_ignores_pre_open_and_stale():
    pre_open = [{"minute_ms": _ms(PIN_DAY, 9, 15)}]
    assert bt.select_decision_point(pre_open, _ms(PIN_DAY, 10, 0)) is None
    stale = [{"minute_ms": _ms(PIN_DAY, 9, 5)}]  # older than DECISION_MAX_AGE_MINUTES
    assert bt.select_decision_point(stale, _ms(PIN_DAY, 10, 0)) is None


# ---------------------------------------------------------------------------
# Scoring math
# ---------------------------------------------------------------------------
def _record(label, agreement, outcome, hit):
    return {"date": "2026-08-19", "label": label, "agreement": agreement,
            "direction": 0, "outcome": outcome, "drift": 0.0, "hit": hit}


def test_score_records_mixed_excluded_from_denominator():
    stats = bt.score_records([
        _record("PIN", 1, "PIN", True),
        _record("PIN", 2, "TREND_DOWN", False),
        _record("MIXED", 1, "TREND_UP", None),
        _record("MIXED", 0, "RANGE", None),
    ])
    assert stats["by_label"]["PIN"] == {"n": 2, "scored": 2, "hits": 1}
    assert stats["by_label"]["MIXED"] == {"n": 2, "scored": 0, "hits": 0}
    # overall denominator counts only the two scored PIN days, not the MIXED days
    assert stats["overall"] == {"n": 4, "scored": 2, "hits": 1, "hit_rate": 0.5}
    # MIXED days still show up in the confusion matrix
    assert stats["confusion"]["MIXED"]["TREND_UP"] == 1
    assert stats["confusion"]["MIXED"]["RANGE"] == 1


def test_score_records_agreement_breakdown():
    stats = bt.score_records([
        _record("PIN", 1, "PIN", True),
        _record("PIN", 1, "TREND_UP", False),
        _record("TRAP_DOOR", 2, "TREND_DOWN", True),
        _record("SQUEEZE", 3, "TREND_UP", True),
        _record("MIXED", 2, "RANGE", None),  # unscored: must not touch agreement stats
    ])
    assert stats["by_agreement"][1] == {"scored": 2, "hits": 1}
    assert stats["by_agreement"][2] == {"scored": 1, "hits": 1}
    assert stats["by_agreement"][3] == {"scored": 1, "hits": 1}
    assert stats["by_agreement"][0] == {"scored": 0, "hits": 0}


# ---------------------------------------------------------------------------
# End-to-end: synthetic multi-day SQLite snapshot store
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE matrix_gex_snapshot (
    symbol TEXT NOT NULL, session_date TEXT NOT NULL, minute_ms INTEGER NOT NULL,
    spot REAL NOT NULL, total_gex REAL NOT NULL DEFAULT 0,
    total_dex REAL NOT NULL DEFAULT 0, total_vex REAL NOT NULL DEFAULT 0,
    total_chex REAL NOT NULL DEFAULT 0,
    flip REAL, call_wall REAL, put_wall REAL, max_gamma_strike REAL,
    regime TEXT, updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (symbol, minute_ms)
)
"""


def _insert_day(db, day, minute_totals, walls=(101.0, 95.0)):
    """minute_totals: [(hour, minute, spot, total_gex), ...] 1-min snapshots."""
    rows = []
    for hour, minute, spot, gex in minute_totals:
        stamp = _ms(day, hour, minute)
        rows.append(("SPY", day.isoformat(), stamp, spot, gex, 0.0, 0.0, 0.0,
                     None, walls[0], walls[1], None, "neutral", stamp))
    db.executemany(
        "INSERT INTO matrix_gex_snapshot (symbol, session_date, minute_ms, spot,"
        " total_gex, total_dex, total_vex, total_chex, flip, call_wall,"
        " put_wall, max_gamma_strike, regime, updated_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.commit()


@pytest.fixture()
def synthetic_db(tmp_path):
    """Three sessions:

    PIN_DAY:  positive GEX near the 101 wall 2 days before OpEx -> PIN label;
              afternoon spots settle at 100.95 (glued to the wall) -> PIN hit.
              A negative-GEX 9:35 snapshot guards decision-time selection.
    PIN_DAY_2: same PIN label setup, but price trap-doors to 97 -> TREND_DOWN,
              a scored miss.
    FAR_DAY:  8 days before OpEx with walls far away -> MIXED (unscored).
    """
    path = tmp_path / "matrix_flow.sqlite3"
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    _insert_day(db, PIN_DAY, [
        (9, 35, 100.2, -5e6),   # early negativity must NOT drive the verdict
        (10, 0, 100.5, 5e6),    # the decision snapshot
        (11, 0, 100.7, 5e6),
        (13, 0, 100.4, 5e6),
        (16, 0, 100.95, 5e6),   # close pinned to the 101 wall
    ])
    _insert_day(db, PIN_DAY_2, [
        (10, 0, 100.5, 5e6),
        (12, 0, 99.0, 5e6),
        (16, 0, 97.0, 5e6),
    ])
    _insert_day(db, FAR_DAY, [
        (10, 0, 100.5, 5e6),
        (16, 0, 101.2, 5e6),
    ], walls=(120.0, 80.0))
    db.close()
    return path


def test_end_to_end_sqlite_hit_rate_math(synthetic_db):
    days = bt.load_db_days(synthetic_db, "SPY")
    assert sorted(days) == [FAR_DAY.isoformat(), PIN_DAY.isoformat(),
                            PIN_DAY_2.isoformat()]
    records, skipped = bt.run_backtest(days, candle_days={}, decision=(10, 0))
    assert skipped == []
    by_date = {r["date"]: r for r in records}

    first = by_date[PIN_DAY.isoformat()]
    assert first["label"] == "PIN"          # positive GEX + at wall + OpEx in 2d
    assert first["outcome"] == "PIN"        # closed at 100.95 vs the 101 wall
    assert first["hit"] is True
    assert first["outcome_source"] == "spot_series"
    assert first["decision_snapshot_age_s"] == 0

    second = by_date[PIN_DAY_2.isoformat()]
    assert second["label"] == "PIN"
    assert second["outcome"] == "TREND_DOWN"
    assert second["hit"] is False

    third = by_date[FAR_DAY.isoformat()]
    assert third["label"] == "MIXED"        # charm too weak 8 days out
    assert third["hit"] is None             # excluded from accuracy

    stats = bt.score_records(records)
    assert stats["overall"]["scored"] == 2  # the MIXED day is not a denominator
    assert stats["overall"]["hits"] == 1
    assert stats["overall"]["hit_rate"] == pytest.approx(0.5)
    assert stats["by_label"]["PIN"] == {"n": 2, "scored": 2, "hits": 1}
    assert stats["confusion"]["PIN"]["PIN"] == 1
    assert stats["confusion"]["PIN"]["TREND_DOWN"] == 1


def test_end_to_end_respects_start_end_and_decision_time(synthetic_db):
    days = bt.load_db_days(synthetic_db, "SPY",
                           start=PIN_DAY.isoformat(), end=PIN_DAY.isoformat())
    assert sorted(days) == [PIN_DAY.isoformat()]
    # a 09:00 decision time predates every snapshot -> day skipped, not scored
    records, skipped = bt.run_backtest(days, decision=(9, 0))
    assert records == [] and skipped and "no snapshot" in skipped[0][1]


def test_end_to_end_candles_drive_outcome_when_available(synthetic_db):
    days = bt.load_db_days(synthetic_db, "SPY",
                           start=PIN_DAY.isoformat(), end=PIN_DAY.isoformat())
    # Candle close far from any wall with full-range drift -> TREND_UP,
    # overriding the spot series (which alone would classify PIN).
    bars = [
        {"time": _ms(PIN_DAY, 10, 0) / 1000, "open": 100.5, "high": 100.6,
         "low": 100.4, "close": 100.55},
        {"time": _ms(PIN_DAY, 15, 0) / 1000, "open": 100.55, "high": 103.0,
         "low": 100.5, "close": 102.9},
    ]
    records, _ = bt.run_backtest(
        days, candle_days={PIN_DAY.isoformat(): bars}, decision=(10, 0))
    assert records[0]["outcome"] == "TREND_UP"
    assert records[0]["outcome_source"] == "candles"
    assert records[0]["hit"] is False  # PIN label did not predict a trend day


# ---------------------------------------------------------------------------
# Phase-5 replay fields (atm_iv / term_slope / deadzone scales)
# ---------------------------------------------------------------------------
SCHEMA_V2 = """
CREATE TABLE matrix_gex_snapshot (
    symbol TEXT NOT NULL, session_date TEXT NOT NULL, minute_ms INTEGER NOT NULL,
    spot REAL NOT NULL, total_gex REAL NOT NULL DEFAULT 0,
    total_dex REAL NOT NULL DEFAULT 0, total_vex REAL NOT NULL DEFAULT 0,
    total_chex REAL NOT NULL DEFAULT 0,
    flip REAL, call_wall REAL, put_wall REAL, max_gamma_strike REAL,
    regime TEXT, atm_iv REAL, term_slope REAL,
    gex_scale REAL NOT NULL DEFAULT 0, vex_scale REAL NOT NULL DEFAULT 0,
    chex_scale REAL NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (symbol, minute_ms)
)
"""


def _insert_day_v2(db, day, minutes, walls=(120.0, 80.0)):
    """minutes: [(hour, minute, spot, gex, vex, chex, term_slope, gex_scale,
    vex_scale, chex_scale), ...] — the post-migration snapshot shape."""
    rows = []
    for hour, minute, spot, gex, vex, chex, slope, gs, vs, cs in minutes:
        stamp = _ms(day, hour, minute)
        rows.append(("SPY", day.isoformat(), stamp, spot, gex, 0.0, vex, chex,
                     None, walls[0], walls[1], None, "neutral", 0.22, slope,
                     gs, vs, cs, stamp))
    db.executemany(
        "INSERT INTO matrix_gex_snapshot (symbol, session_date, minute_ms, spot,"
        " total_gex, total_dex, total_vex, total_chex, flip, call_wall,"
        " put_wall, max_gamma_strike, regime, atm_iv, term_slope, gex_scale,"
        " vex_scale, chex_scale, updated_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.commit()


def test_engine_inputs_consumes_phase5_columns():
    point = {
        "minute_ms": _ms(PIN_DAY, 10, 0), "spot": 100.5,
        "total_gex": -5e6, "total_dex": 0.0, "total_vex": -1e6, "total_chex": 0.0,
        "flip": None, "call_wall": 120.0, "put_wall": 80.0,
        "atm_iv": 0.22, "term_slope": -0.03,
        "gex_scale": 6e6, "vex_scale": 2e6, "chex_scale": 1e6,
    }
    inputs = bt.engine_inputs(point, _ms(PIN_DAY, 10, 0))
    assert inputs["atm_iv"] == 0.22
    assert inputs["term_slope"] == -0.03
    assert inputs["gex_scale"] == 6e6
    assert inputs["vex_scale"] == 2e6
    assert inputs["chex_scale"] == 1e6

    # Legacy rows (NULL vol fields, zero scales) omit them entirely -> the
    # engine's neutral fallback (zero deadzones, VEX leg unavailable).
    legacy = {**point, "atm_iv": None, "term_slope": None,
              "gex_scale": 0.0, "vex_scale": 0.0, "chex_scale": 0.0}
    inputs = bt.engine_inputs(legacy, _ms(PIN_DAY, 10, 0))
    for key in ("atm_iv", "term_slope", "gex_scale", "vex_scale", "chex_scale"):
        assert key not in inputs


def test_phase5_columns_make_all_labels_reachable(tmp_path):
    """With persisted term_slope + scales, the VEX leg replays and
    GRIND_UP / SQUEEZE / TRAP_DOOR all fire from stored history."""
    path = tmp_path / "matrix_flow.sqlite3"
    db = sqlite3.connect(path)
    db.execute(SCHEMA_V2)
    # GRIND_UP: positive gamma + backwardation (IV falling, vanna bid) +
    # charm bid 2 days before OpEx. Walls far away so PIN cannot preempt it.
    _insert_day_v2(db, PIN_DAY, [
        (10, 0, 100.5, 5e6, -1e6, 5e4, 0.02, 6e6, 2e6, 1e6),
        (16, 0, 102.5, 5e6, -1e6, 5e4, 0.02, 6e6, 2e6, 1e6),
    ])
    # SQUEEZE: IV collapsing hard out of backwardation into a vanna bid while
    # gamma is negative.
    _insert_day_v2(db, PIN_DAY_2, [
        (10, 0, 100.5, -5e6, -1e6, 0.0, 0.04, 6e6, 2e6, 1e6),
        (16, 0, 102.0, -5e6, -1e6, 0.0, 0.04, 6e6, 2e6, 1e6),
    ])
    # TRAP_DOOR: negative gamma + steep contango (IV rising, vanna supply).
    _insert_day_v2(db, FAR_DAY, [
        (10, 0, 100.5, -5e6, -1e6, 0.0, -0.03, 6e6, 2e6, 1e6),
        (16, 0, 97.5, -5e6, -1e6, 0.0, -0.03, 6e6, 2e6, 1e6),
    ])
    db.close()

    days = bt.load_db_days(path, "SPY")
    records, skipped = bt.run_backtest(days, decision=(10, 0))
    assert skipped == []
    by_date = {r["date"]: r for r in records}

    first = by_date[PIN_DAY.isoformat()]
    assert first["label"] == "GRIND_UP"
    assert first["outcome"] == "TREND_UP"
    assert first["hit"] is True

    second = by_date[PIN_DAY_2.isoformat()]
    assert second["label"] == "SQUEEZE"
    assert second["outcome"] == "TREND_UP"
    assert second["hit"] is True

    third = by_date[FAR_DAY.isoformat()]
    assert third["label"] == "TRAP_DOOR"
    assert third["outcome"] == "TREND_DOWN"
    assert third["hit"] is True

    stats = bt.score_records(records)
    assert stats["overall"]["scored"] == 3
    assert stats["overall"]["hits"] == 3


def test_phase5_legacy_rows_keep_neutral_vex(tmp_path):
    """Pre-migration rows (NULL slope, zero scales) replay with a neutral VEX
    force: the same TRAP_DOOR-shaped totals can only produce MIXED."""
    path = tmp_path / "matrix_flow.sqlite3"
    db = sqlite3.connect(path)
    db.execute(SCHEMA_V2)
    for slope, scales_day in ((None, FAR_DAY),):
        _insert_day_v2(db, scales_day, [
            (10, 0, 100.5, -5e6, -1e6, 0.0, slope, 0.0, 0.0, 0.0),
            (16, 0, 97.5, -5e6, -1e6, 0.0, slope, 0.0, 0.0, 0.0),
        ])
    db.close()

    days = bt.load_db_days(path, "SPY")
    records, skipped = bt.run_backtest(days, decision=(10, 0))
    assert skipped == []
    assert records[0]["label"] == "MIXED"  # VEX neutral -> TRAP_DOOR unreachable
    assert records[0]["hit"] is None       # MIXED excluded from accuracy


# ---------------------------------------------------------------------------
# exposure_history JSON fallback
# ---------------------------------------------------------------------------
def test_history_source_totals_and_walls(tmp_path):
    day_dir = tmp_path / "exposure_history" / PIN_DAY.isoformat()
    day_dir.mkdir(parents=True)
    payload = {
        "date": PIN_DAY.isoformat(), "symbol": "SPY",
        "snapshots": [{
            "asof": f"{PIN_DAY.isoformat()}T10:00:00",  # naive ET, as collected
            "spot": 100.5,
            "rows": [
                [101, 8e6, -1e6, 3e5, -2e5],   # largest net GEX -> call wall
                [95, 1e6, -7e6, 1e5, -4e5],   # most negative -> put wall
            ],
        }, {
            "asof": f"{PIN_DAY.isoformat()}T15:30:00",
            "spot": 100.9,
            "rows": [[101, 8e6, -1e6, 0, 0], [95, 1e6, -7e6, 0, 0]],
        }],
    }
    (day_dir / "SPY.json").write_text(json.dumps(payload))

    days = bt.load_history_days(tmp_path / "exposure_history", "SPY")
    points = days[PIN_DAY.isoformat()]
    assert len(points) == 2
    point = points[0]
    assert point["minute_ms"] == _ms(PIN_DAY, 10, 0)
    assert point["total_gex"] == pytest.approx(8e6 - 1e6 + 1e6 - 7e6)
    assert point["total_dex"] == pytest.approx(3e5 - 2e5 + 1e5 - 4e5)
    assert point["call_wall"] == 101 and point["put_wall"] == 95
    assert point["gex_scale"] > 0

    records, skipped = bt.run_backtest(days, decision=(10, 0))
    assert skipped == []
    assert records[0]["label"] == "PIN"     # wall-adjacent, OpEx in 2 days
    assert records[0]["outcome"] == "PIN"   # 15:30 spot 100.9 glued to 101
    assert records[0]["hit"] is True


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------
def test_main_json_and_text(tmp_path, synthetic_db, capsys):
    code = bt.main(["--symbol", "SPY", "--db", str(synthetic_db),
                    "--candles", str(tmp_path / "absent.json"), "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["symbol"] == "SPY"
    assert report["sessions"] == 3
    assert report["overall"]["hit_rate"] == pytest.approx(0.5)
    assert len(report["records"]) == 3

    code = bt.main(["--symbol", "SPY", "--db", str(synthetic_db),
                    "--candles", str(tmp_path / "absent.json")])
    assert code == 0
    text = capsys.readouterr().out
    assert "Hit rate by label" in text
    assert "CAUTION" in text  # 2 scored days << SMALL_SAMPLE_DAYS


def test_main_reports_missing_source(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = bt.main(["--symbol", "SPY", "--db", str(tmp_path / "nope.sqlite3"),
                    "--source", "db"])
    assert code == 2
    assert "not found" in capsys.readouterr().err


def test_parse_decision_time_validation():
    assert bt.parse_decision_time("10:00") == (10, 0)
    assert bt.parse_decision_time("9:45") == (9, 45)
    for bad in ("10", "10:60", "25:00", "ten-oclock"):
        with pytest.raises(ValueError):
            bt.parse_decision_time(bad)
