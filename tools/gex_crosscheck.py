"""Cross-check our daily Naive GEX against SqueezeMetrics' free daily GEX.

A calculation bug in Naive GEX should surface here before any conclusion is
drawn from the GEX Filter. SqueezeMetrics publishes a daily GEX for the S&P 500
in its DIX CSV (columns date, price, dix, gex). Their scale and construction
differ from ours, so the check is scale-free: over the sessions both series
cover, the Pearson correlation of the two and the share of sessions on which
they agree on the sign (the Gamma Regime).

Our series is the one the GEX Filter trades on: Naive GEX at each session's
10:00 ET entry snapshot, from tools/backtest_spread.py.

Usage:
    python tools/gex_crosscheck.py --store PATH [--csv PATH_OR_URL]
        [--start DATE] [--end DATE] [--out crosscheck.json]

The JSON report is printed to stdout (and written to --out when given). Exits 1
when fewer than two sessions overlap.
"""
import argparse
import csv
import importlib.util
import io
import json
import sqlite3
import statistics
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("backtest_spread", TOOLS / "backtest_spread.py")
backtest_spread = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backtest_spread)
chain_store = backtest_spread.chain_store

SQUEEZEMETRICS_CSV = "https://squeezemetrics.com/monitor/static/DIX.csv"
MIN_OVERLAP = 2  # sessions; a correlation needs at least two points
HTTP_TIMEOUT_SECONDS = 60


def read_squeezemetrics(source):
    """{date: gex} from a SqueezeMetrics DIX CSV at a path or URL."""
    if str(source).startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=HTTP_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8")
    else:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"SqueezeMetrics CSV not found: {path}")
        text = path.read_text()
    return {date.fromisoformat(row["date"]): float(row["gex"])
            for row in csv.DictReader(io.StringIO(text)) if row.get("gex")}


def store_span(store_path):
    """(first, last) session date in the chain store, or None when empty."""
    store_path = Path(store_path)
    if not store_path.exists():
        raise FileNotFoundError(f"chain store not found: {store_path}")
    connection = chain_store.connect(store_path)
    try:
        span = chain_store.snapshot_span(connection)
    finally:
        connection.close()
    if span is None:
        return None
    return tuple(datetime.fromtimestamp(ms / 1000, tz=backtest_spread.ET).date()
                 for ms in span)


def _sign(value):
    return (value > 0) - (value < 0)


def crosscheck(ours, theirs):
    """Correlation and sign agreement over the sessions both series cover."""
    days = sorted(ours.keys() & theirs.keys())
    report = {"days": len(days),
              "first": days[0].isoformat() if days else None,
              "last": days[-1].isoformat() if days else None,
              "correlation": None, "sign_agreement": None}
    if len(days) < MIN_OVERLAP:
        return report
    x, y = [ours[d] for d in days], [theirs[d] for d in days]
    try:
        report["correlation"] = statistics.correlation(x, y)
    except statistics.StatisticsError:  # one series is constant
        pass
    report["sign_agreement"] = sum(_sign(a) == _sign(b) for a, b in zip(x, y)) / len(days)
    return report


def run(store_path, csv_source, start=None, end=None):
    theirs = read_squeezemetrics(csv_source)
    span = store_span(store_path)
    ours = {}
    if span is not None and theirs:
        first = max(d for d in (span[0], min(theirs), start) if d is not None)
        last = min(d for d in (span[1], max(theirs), end) if d is not None)
        if first <= last:
            ours = backtest_spread.gex_series(store_path, first, last)
    return crosscheck(ours, theirs)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare our daily Naive GEX with SqueezeMetrics' daily GEX.")
    parser.add_argument("--store", required=True, help="Path to the chain store SQLite file")
    parser.add_argument("--csv", default=SQUEEZEMETRICS_CSV,
                        help=f"SqueezeMetrics DIX CSV, path or URL (default {SQUEEZEMETRICS_CSV})")
    parser.add_argument("--start", type=date.fromisoformat,
                        help="First session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="Last session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--out", help="Also write the JSON report to this path")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        report = run(args.store, args.csv, args.start, args.end)
    except (FileNotFoundError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(f"gex_crosscheck: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0 if report["correlation"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
