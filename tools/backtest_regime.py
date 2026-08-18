"""Backtest the matrix regime engine against what sessions actually did.

For each trading day in the stored history, this replays the regime inputs as
of a fixed decision time (default 10:00 ET, after the opening noise settles),
runs tripity_experiment.matrix_regime.compute_regime, and scores the returned
label against the realized day outcome. The goal is to answer "when the engine
says GRIND_UP / TRAP_DOOR / PIN / SQUEEZE / MIXED, how often is it right?" so
weak rules can be fixed or removed.

Usage:
    python tools/backtest_regime.py --symbol SPY [--db PATH] [--start DATE]
        [--end DATE] [--decision-time HH:MM] [--candles PATH]
        [--history-root DIR] [--source auto|db|history] [--json]

Data sources (auto-detected, or force with --source):
- db: the Railway SQLite snapshot store (matrix_flow.sqlite3,
  matrix_gex_snapshot table: 1-minute spot + total_gex/dex/vex/chex + flip +
  walls per symbol/session, plus — for snapshots written after the Phase-5
  migration — atm_iv, term_slope and the per-force deadzone scales
  gex_scale/vex_scale/chex_scale). Default path mirrors the host: env
  TRIPITY_MATRIX_FLOW_DB, else ./matrix_flow.sqlite3, else
  /data/matrix_flow.sqlite3.
- history: exposure_history/<date>/<SYMBOL>.json files written by
  tools/build_exposure_history.py (30-minute per-strike GEX/DEX snapshots;
  VEX/CHEX are not stored there and replay as zero).

Day outcomes come from --candles (a candles_data.json-style file of intraday
OHLC bars, the same feed the /matrix/regime endpoint uses) when it covers the
session; otherwise from the source's own spot series after the decision time.

REPLAY NOTES: no history store persists the option chain, so the VRP leg
(realized vol) always replays as unavailable. In the snapshot DB, rows
written after the Phase-5 migration carry atm_iv + term_slope + the three
deadzone scales, which reactivates the VEX force and makes TRAP_DOOR /
SQUEEZE / GRIND_UP reachable from stored history; legacy rows (NULL slope,
zero scales) still replay with a neutral VEX force and zero deadzones, so
from those rows only PIN and MIXED can fire. The exposure_history fallback
stores no vol data at all, so there VEX always replays as neutral.

Outcome classification and the label->outcome prediction mapping live in
tripity_experiment.matrix_outcome (shared with the journal grader in
public_company_host.py — the rules exist exactly once). See that module's
docstring for the explicit named-constant rules.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
from tripity_experiment import matrix_regime
# Shared outcome rules (canonical home: tripity_experiment.matrix_outcome).
# Re-exported here so existing callers/tests keep working unchanged.
from tripity_experiment.matrix_outcome import (  # noqa: F401
    OUTCOME_PIN_WALL_PCT,
    OUTCOMES,
    TREND_EFFICIENCY,
    TREND_MIN_DRIFT_PCT,
    classify_outcome,
    ohlc_from_bars,
    ohlc_from_spots,
    predicts,
)

ET = ZoneInfo("America/New_York")

# --- Decision-time rules (named constants; tune here only) ---
DEFAULT_DECISION_TIME = "10:00"  # ET; opening noise has settled by then
# A snapshot this many minutes older than the decision time is too stale to
# replay (exposure_history buckets are 30 min, so 45 keeps them eligible).
DECISION_MAX_AGE_MINUTES = 45
MARKET_OPEN = (9, 30)  # ET; snapshots before this never count as the decision

# Accuracy on fewer scored days than this is noise, not signal.
SMALL_SAMPLE_DAYS = 30

SNAPSHOT_COLUMNS = (
    "minute_ms", "spot", "total_gex", "total_dex", "total_vex", "total_chex",
    "flip", "call_wall", "put_wall",
    # Phase-5 additions (absent in legacy DBs; NULL/zero in migrated rows):
    "atm_iv", "term_slope", "gex_scale", "vex_scale", "chex_scale",
)


# ---------------------------------------------------------------------------
# Loading: SQLite snapshot store
# ---------------------------------------------------------------------------
def default_db_path():
    """Mirror public_company_host._matrix_flow_db_path."""
    configured = os.getenv("TRIPITY_MATRIX_FLOW_DB", "").strip()
    if configured:
        return Path(configured)
    data_dir = Path("/data")
    return (data_dir if data_dir.exists() else Path.cwd()) / "matrix_flow.sqlite3"


def load_db_days(db_path, symbol, start=None, end=None):
    """Read matrix_gex_snapshot into {session_date: [point, ...]} (time order).

    point keys: minute_ms, spot, total_gex, total_dex, total_vex, total_chex,
    flip, call_wall, put_wall, plus the Phase-5 replay fields atm_iv,
    term_slope, gex_scale, vex_scale, chex_scale when the DB has them.
    Tolerates legacy DBs missing any of the newer columns (fields fill as
    None, which replays as the neutral fallback).
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"snapshot DB not found: {db_path}")
    with sqlite3.connect(db_path, timeout=20) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "matrix_gex_snapshot" not in tables:
            raise ValueError(f"{db_path} has no matrix_gex_snapshot table")
        available = {row[1] for row in connection.execute(
            "PRAGMA table_info(matrix_gex_snapshot)")}
        columns = [c for c in SNAPSHOT_COLUMNS if c in available]
        query = (
            f"SELECT {', '.join(columns)} FROM matrix_gex_snapshot"
            " WHERE symbol = ?"
        )
        params = [symbol]
        if start:
            query += " AND session_date >= ?"
            params.append(start)
        if end:
            query += " AND session_date <= ?"
            params.append(end)
        query += " ORDER BY minute_ms"
        rows = connection.execute(query, params).fetchall()
    days = {}
    for row in rows:
        point = {name: row[i] for i, name in enumerate(columns)}
        for missing in set(SNAPSHOT_COLUMNS) - set(columns):
            point[missing] = 0 if missing.startswith("total_") else None
        days.setdefault(_session_of(point["minute_ms"]), []).append(point)
    return days


def _session_of(minute_ms):
    return datetime.fromtimestamp(minute_ms / 1000, tz=ET).date().isoformat()


# ---------------------------------------------------------------------------
# Loading: exposure_history/ JSON fallback
# ---------------------------------------------------------------------------
def load_history_days(root, symbol, start=None, end=None):
    """Read exposure_history/<date>/<SYMBOL>.json into the same day shape.

    Snapshot rows are [strike, call_gex, put_gex, call_dex, put_dex] (puts are
    stored sign-adjusted). VEX/CHEX are not persisted there -> replay as 0.
    Walls mirror the host convention: call_wall = strike with the largest net
    GEX, put_wall = the most negative. gex_scale (sum of |call|+|put| per
    strike) is recovered so the GEX noise band matches the live engine.
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"exposure history root not found: {root}")
    days = {}
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir() or len(day_dir.name) != 10:
            continue
        if start and day_dir.name < start or end and day_dir.name > end:
            continue
        target = day_dir / f"{symbol}.json"
        if not target.exists():
            continue
        try:
            payload = json.loads(target.read_text())
        except (ValueError, OSError):
            continue
        points = []
        for snap in payload.get("snapshots") or []:
            point = _history_point(snap)
            if point:
                points.append(point)
        if points:
            days[day_dir.name] = sorted(points, key=lambda p: p["minute_ms"])
    return days


def _history_point(snap):
    spot = float(snap.get("spot") or 0)
    if spot <= 0:
        return None
    asof = str(snap.get("asof") or "")
    try:
        stamp = datetime.fromisoformat(asof)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ET)  # collector writes naive ET
    total_gex = total_dex = gex_scale = 0.0
    call_wall = put_wall = None
    best_call, best_put = float("-inf"), float("inf")
    for row in snap.get("rows") or []:
        try:
            strike, call_gex, put_gex, call_dex, put_dex = (float(v) for v in row[:5])
        except (TypeError, ValueError):
            continue
        total_gex += call_gex + put_gex
        total_dex += call_dex + put_dex
        gex_scale += abs(call_gex) + abs(put_gex)
        net = call_gex + put_gex
        if net > best_call:
            best_call, call_wall = net, strike
        if net < best_put:
            best_put, put_wall = net, strike
    return {
        "minute_ms": int(stamp.timestamp() * 1000),
        "spot": spot, "total_gex": total_gex, "total_dex": total_dex,
        "total_vex": 0.0, "total_chex": 0.0,
        "flip": None, "call_wall": call_wall, "put_wall": put_wall,
        "gex_scale": gex_scale,
    }


# ---------------------------------------------------------------------------
# Candles (day outcomes, same feed the regime endpoint uses)
# ---------------------------------------------------------------------------
def load_candle_days(path, symbol):
    """Load a candles_data.json-style file into {session_date: [bar, ...]}.

    Bars are {"time": epoch_seconds, "open", "high", "low", "close"} intraday
    OHLC (any interval). Missing file/symbol simply yields no candle coverage.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    record = payload.get(symbol)
    if not isinstance(record, dict):
        return {}
    days = {}
    for bar in record.get("candles") or []:
        try:
            stamp = float(bar["time"])
            o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0 or h < l:
            continue
        session = datetime.fromtimestamp(stamp, tz=ET).date().isoformat()
        days.setdefault(session, []).append(
            {"time": stamp, "open": o, "high": h, "low": l, "close": c})
    for bars in days.values():
        bars.sort(key=lambda b: b["time"])
    return days


# ---------------------------------------------------------------------------
# Outcome classification + OHLC shaping: imported from
# tripity_experiment.matrix_outcome (shared with the journal grader). The
# module-level names stay available here for backwards compatibility.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The replay loop
# ---------------------------------------------------------------------------
def _et_ms(day, hour, minute):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


def parse_decision_time(text):
    try:
        hour, minute = (int(part) for part in str(text).split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except ValueError:
        raise ValueError(f"invalid --decision-time {text!r} (want HH:MM)")


def select_decision_point(points, decision_ms):
    """Latest snapshot at/before the decision time (and inside the session).

    Returns None when nothing qualifies or the newest candidate is staler than
    DECISION_MAX_AGE_MINUTES — replaying a stale snapshot would mislabel the day.
    """
    open_ms = _et_ms(datetime.fromtimestamp(decision_ms / 1000, tz=ET).date(),
                     *MARKET_OPEN)
    candidates = [p for p in points if open_ms <= p["minute_ms"] <= decision_ms]
    if not candidates:
        return None
    point = max(candidates, key=lambda p: p["minute_ms"])
    age_ms = decision_ms - point["minute_ms"]
    if age_ms > DECISION_MAX_AGE_MINUTES * 60 * 1000:
        return None
    return point


def engine_inputs(point, decision_ms):
    """Regime inputs replayed from one stored snapshot.

    No chain or candles are persisted in either history store, so the VRP leg
    always replays as unavailable. Snapshot rows written after the Phase-5
    migration carry atm_iv/term_slope and the per-force deadzone scales,
    which reactivates the VEX force (TRAP_DOOR/SQUEEZE/GRIND_UP become
    reachable); legacy rows replay with a neutral VEX force and zero
    deadzones — see the module docstring.
    """
    inputs = {
        "spot": point["spot"],
        "total_gex": point["total_gex"],
        "total_dex": point["total_dex"],
        "total_vex": point["total_vex"],
        "total_chex": point["total_chex"],
        "flip": point["flip"],
        "call_wall": point["call_wall"],
        "put_wall": point["put_wall"],
        "valuation_ms": decision_ms,
    }
    for key in ("gex_scale", "vex_scale", "chex_scale"):
        if point.get(key):
            inputs[key] = point[key]
    if point.get("atm_iv") is not None:
        inputs["atm_iv"] = point["atm_iv"]
    if point.get("term_slope") is not None:
        inputs["term_slope"] = point["term_slope"]
    return inputs


def run_backtest(days, candle_days=None, decision=(10, 0)):
    """Replay every session; return (records, skipped).

    record: {date, label, agreement, direction, outcome, drift, hit,
             outcome_source, decision_snapshot_age_s}.
    skipped: [(date, reason), ...] for days that could not be replayed.
    """
    candle_days = candle_days or {}
    records, skipped = [], []
    for session in sorted(days):
        day = date.fromisoformat(session)
        decision_ms = _et_ms(day, *decision)
        point = select_decision_point(days[session], decision_ms)
        if point is None:
            skipped.append((session, "no snapshot at/before decision time"))
            continue
        bars = [b for b in candle_days.get(session, [])
                if b["time"] * 1000 >= decision_ms]
        ohlc = ohlc_from_bars(bars)
        outcome_source = "candles"
        if ohlc is None:
            after = [p for p in days[session] if p["minute_ms"] >= point["minute_ms"]]
            ohlc = ohlc_from_spots(after)
            outcome_source = "spot_series"
        if ohlc is None:
            skipped.append((session, "no prices after decision time"))
            continue
        verdict = matrix_regime.compute_regime(engine_inputs(point, decision_ms))
        outcome, drift, wall = classify_outcome(
            *ohlc, walls=(point["call_wall"], point["put_wall"]))
        records.append({
            "date": session,
            "label": verdict["label"],
            "agreement": verdict["agreement"],
            "direction": verdict["direction"],
            "outcome": outcome,
            "drift": round(drift, 6),
            "wall_hit": wall,
            "hit": predicts(verdict["label"], outcome, drift),
            "outcome_source": outcome_source,
            "decision_snapshot_age_s": int((decision_ms - point["minute_ms"]) / 1000),
        })
    return records, skipped


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_records(records):
    """Hit rates per label, per force-agreement level, and a confusion matrix.

    MIXED makes no prediction: it appears in the confusion matrix but is
    excluded from every hit-rate denominator.
    """
    by_label = {label: {"n": 0, "scored": 0, "hits": 0}
                for label in matrix_regime.LABELS}
    by_agreement = {level: {"scored": 0, "hits": 0} for level in range(4)}
    confusion = {label: {outcome: 0 for outcome in OUTCOMES}
                 for label in matrix_regime.LABELS}
    for record in records:
        label = record["label"]
        by_label[label]["n"] += 1
        confusion[label][record["outcome"]] += 1
        if record["hit"] is None:
            continue
        by_label[label]["scored"] += 1
        by_label[label]["hits"] += int(record["hit"])
        level = by_agreement[record["agreement"]]
        level["scored"] += 1
        level["hits"] += int(record["hit"])
    scored = sum(v["scored"] for v in by_label.values())
    hits = sum(v["hits"] for v in by_label.values())
    return {
        "by_label": by_label,
        "by_agreement": by_agreement,
        "confusion": confusion,
        "overall": {"n": len(records), "scored": scored, "hits": hits,
                    "hit_rate": (hits / scored) if scored else None},
    }


def _rate(hits, scored):
    return f"{hits / scored * 100:5.1f}%" if scored else "  n/a"


def format_report(symbol, source_desc, decision, records, skipped, stats):
    """Human-readable backtest report."""
    hour, minute = decision
    lines = [
        f"Matrix regime backtest - {symbol}",
        f"Source: {source_desc}",
        f"Decision time: {hour:02d}:{minute:02d} ET | sessions replayed: "
        f"{len(records)}"
        + (f" ({records[0]['date']} .. {records[-1]['date']})" if records else ""),
    ]
    if skipped:
        lines.append(f"Skipped {len(skipped)} session(s): "
                     + "; ".join(f"{d} ({why})" for d, why in skipped))
    lines.append(
        "NOTE: only snapshot rows persisted after the Phase-5 migration carry "
        "atm_iv/term_slope/scales; older rows (and exposure_history) replay "
        "with a neutral VEX force, so TRAP_DOOR/SQUEEZE/GRIND_UP can only "
        "fire on sessions with migrated snapshots.")
    lines.append("")
    lines.append("Hit rate by label (MIXED makes no prediction -> excluded):")
    lines.append(f"  {'label':<10}{'days':>5}{'scored':>8}{'hits':>6}{'hit%':>8}")
    for label in matrix_regime.LABELS:
        row = stats["by_label"][label]
        if not row["n"]:
            continue
        if row["scored"]:
            lines.append(f"  {label:<10}{row['n']:>5}{row['scored']:>8}"
                         f"{row['hits']:>6}{_rate(row['hits'], row['scored']):>8}")
        else:
            lines.append(f"  {label:<10}{row['n']:>5}{'-':>8}{'-':>6}{'no prediction':>8}")
    lines.append("")
    lines.append("Hit rate by force agreement (does alignment help?):")
    for level in range(3, 0, -1):
        row = stats["by_agreement"][level]
        lines.append(f"  {level}/3 aligned: {row['hits']}/{row['scored']} "
                     f"= {_rate(row['hits'], row['scored'])}")
    zero = stats["by_agreement"][0]
    if zero["scored"]:
        lines.append(f"  0/3 aligned: {zero['hits']}/{zero['scored']} "
                     f"= {_rate(zero['hits'], zero['scored'])}")
    lines.append("")
    lines.append("Confusion matrix (rows = engine label, cols = actual outcome):")
    lines.append(f"  {'':<10}" + "".join(f"{o:>11}" for o in OUTCOMES))
    for label in matrix_regime.LABELS:
        if not stats["by_label"][label]["n"]:
            continue
        row = stats["confusion"][label]
        lines.append(f"  {label:<10}" + "".join(f"{row[o]:>11}" for o in OUTCOMES))
    lines.append("")
    overall = stats["overall"]
    lines.append(f"Overall accuracy (excl. MIXED): {overall['hits']}/"
                 f"{overall['scored']} = {_rate(overall['hits'], overall['scored'])}")
    if overall["scored"] and overall["scored"] < SMALL_SAMPLE_DAYS:
        lines.append(f"CAUTION: fewer than {SMALL_SAMPLE_DAYS} scored days - "
                     "small-sample noise dominates; do not tune rules on this.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Backtest the matrix regime engine against realized day outcomes.")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--db", help="Path to matrix_flow.sqlite3 (default: "
                        "env TRIPITY_MATRIX_FLOW_DB, ./matrix_flow.sqlite3, /data)")
    parser.add_argument("--start", help="First session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--end", help="Last session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--decision-time", default=DEFAULT_DECISION_TIME,
                        help="ET time the regime inputs are snapshotted (HH:MM)")
    parser.add_argument("--candles", default="candles_data.json",
                        help="candles_data.json-style OHLC file for day outcomes")
    parser.add_argument("--history-root", default="exposure_history",
                        help="exposure_history/ dir written by build_exposure_history.py")
    parser.add_argument("--source", choices=("auto", "db", "history"), default="auto")
    parser.add_argument("--json", action="store_true",
                        help="Emit the machine-readable report instead of text")
    return parser.parse_args(argv)


def resolve_source(args):
    """Pick db vs history. auto: db if the file exists, else history, else error."""
    db_path = Path(args.db) if args.db else default_db_path()
    history_root = Path(args.history_root)
    if args.source == "db" or (args.source == "auto" and db_path.exists()):
        return "db", db_path, f"sqlite {db_path} (matrix_gex_snapshot)"
    if args.source == "history" or (args.source == "auto" and history_root.is_dir()):
        return "history", history_root, f"exposure_history JSON at {history_root}"
    raise FileNotFoundError(
        "no history found: pass --db PATH to a matrix_flow.sqlite3 snapshot "
        "store or --history-root DIR to an exposure_history/ checkout")


def main(argv=None):
    args = parse_args(argv)
    symbol = args.symbol.upper()
    decision = parse_decision_time(args.decision_time)
    for bound in (args.start, args.end):
        if bound:
            date.fromisoformat(bound)  # raises cleanly on malformed input
    try:
        source, path, source_desc = resolve_source(args)
        if source == "db":
            days = load_db_days(path, symbol, args.start, args.end)
        else:
            days = load_history_days(path, symbol, args.start, args.end)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"backtest_regime: {exc}", file=sys.stderr)
        return 2
    candle_days = load_candle_days(args.candles, symbol) if args.candles else {}
    records, skipped = run_backtest(days, candle_days, decision)
    stats = score_records(records)
    if args.json:
        print(json.dumps({
            "symbol": symbol, "source": source_desc,
            "decision_time": f"{decision[0]:02d}:{decision[1]:02d}",
            "sessions": len(records), "skipped": skipped,
            **stats, "records": records,
        }, indent=2))
    else:
        print(format_report(symbol, source_desc, decision, records, skipped, stats))
    return 0 if records else 1


if __name__ == "__main__":
    sys.exit(main())
