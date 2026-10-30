"""Import an end-of-day option chain archive into the chain store.

The archive is the free US options history first published as
philippdubach/options-data (2008-2025, 100+ symbols). Its upstream is gone;
lambdaclass/options_backtester mirrors SPY, QQQ and IWM on GitHub Releases,
pinned by SHA-256 (EOD_HASHES below):

    https://github.com/lambdaclass/options_backtester/releases/download/data-v1/SPY_options.parquet
    https://github.com/lambdaclass/options_backtester/releases/download/data-v1/SPY_underlying.parquet

Two files per symbol: `<SYM>_options.parquet` (one row per contract per day:
bid, ask, IV, greeks, open interest, volume) and `<SYM>_underlying.parquet`
(daily OHLC, dividends by ex-date). The import writes:

- option_chain_snapshot: one snapshot per day, stamped at 16:00 ET (the
  archive is end-of-day), for expiries 0-max_dte days out and strikes within
  strike_band of the close. iv, vendor_delta and vendor_gamma are the
  archive's; underlying is the day's close; sod_oi is the archive's open
  interest (see the caveat below);
- daily_close and dividend for the symbol, from the underlying file;
- with --vix, the free Cboe VIX closes (the GEX filter's control needs them).

Caveats, as the archive does not document them:
- Open interest is read as known by the session's close. Exchanges publish
  open interest overnight, so an archive that stamps it on the day it was
  published is look-ahead free at the close; one that stamps it on the day
  whose close it is would leak one day. Either way it is never later than
  the next morning.
- Quotes are end-of-day: a backtest on this store enters at the close
  (backtest_options.py --entry-time 16:00), not at 10:00.

Our own archive: tools/archive_cboe_chain.py saves one Cboe snapshot per
session in the same columns, plus the spot it was taken against. --archive
imports `<DIR>/<SYMBOL>/*.csv.gz`; the snapshot's spot becomes the day's
close, and --yahoo adds Yahoo's closes and dividends (for early assignment).

Usage:
    python tools/import_eod_chain.py --store PATH --options SPY_options.parquet
        --underlying SPY_underlying.parquet [--symbol SPY] [--max-dte 45]
        [--strike-band 0.25] [--start DATE] [--end DATE] [--vix] [--verify]
    python tools/import_eod_chain.py --store PATH --archive chain_archive
        --symbol SPY [--yahoo] [--vix] [--start DATE] [--end DATE]
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tripity_experiment import chain_store
from tripity_experiment import matrix_gex

ET = matrix_gex.ET
SNAPSHOT_TIME = (16, 0)  # ET; the archive is end-of-day
DEFAULT_MAX_DTE = 45  # covers the GEX horizon and the 7DTE entries
DEFAULT_STRIKE_BAND = 0.25  # strikes within 25% of the close
OPTION_COLUMNS = ["date", "expiration", "strike", "type", "bid", "ask",
                  "implied_volatility", "delta", "gamma", "open_interest"]
# SHA-256 of the lambdaclass data-v1 release files.
EOD_HASHES = {
    "SPY_options.parquet": "a7152991b45b81f090f970e945bf88def8093b8ecb9b250e9891cb6d88041f0a",
    "SPY_underlying.parquet": "847e60a441eb10969d87cd4a6da604257b782d9076168f68d5730b84096c79db",
    "QQQ_options.parquet": "1f831556cd87ec9d7af43d3b47a69a829ffb2fb199cfcef4ff55d289badf8734",
    "QQQ_underlying.parquet": "78c015fd7fa9d493cd798d5ae81b3c2ae6dc3b8176e25e7bb90cd4114b103351",
    "IWM_options.parquet": "d16a728116f8095c97c39ed66e75000bf66a0ab422b678002eb9f6086fd10ee5",
    "IWM_underlying.parquet": "e980f50473a8c346ba9ac8b05dcf3da691581dae742f649cb3608d0efe2ef940",
}


def _number(value):
    if value is None or pd.isna(value):
        return None
    return float(value)


def snapshot_ms(day):
    return int(datetime(day.year, day.month, day.day, *SNAPSHOT_TIME,
                        tzinfo=ET).timestamp() * 1000)


def read_underlying(path):
    """({date: close}, {ex_date: dividend}) from an underlying file."""
    frame = pd.read_parquet(path, columns=["date", "close", "dividend_amount"])
    days = pd.to_datetime(frame["date"]).dt.date
    closes = {d: float(c) for d, c in zip(days, frame["close"]) if pd.notna(c) and c > 0}
    dividends = {d: float(a) for d, a in zip(days, frame["dividend_amount"])
                 if pd.notna(a) and a > 0}
    return closes, dividends


def chain_rows(frame, symbol, closes, max_dte, strike_band):
    """ChainRows for one frame of archive rows; rows on days without a close,
    outside 0-max_dte days, or outside the strike band are dropped."""
    rows = []
    days = pd.to_datetime(frame["date"]).dt.date
    expiries = pd.to_datetime(frame["expiration"]).dt.date
    for day, expiry, record in zip(days, expiries, frame.itertuples(index=False)):
        close = closes.get(day)
        if close is None or not 0 <= (expiry - day).days <= max_dte:
            continue
        if abs(record.strike / close - 1) > strike_band:
            continue
        rows.append(chain_store.ChainRow(
            root=symbol, expiry=expiry.isoformat(), strike=float(record.strike),
            right="C" if str(record.type).lower().startswith("c") else "P",
            snapshot_ms=snapshot_ms(day), bid=_number(record.bid), ask=_number(record.ask),
            iv=_number(record.implied_volatility), sod_oi=_number(record.open_interest),
            vendor_delta=_number(record.delta), vendor_gamma=_number(record.gamma),
            underlying=close))
    return rows


def import_chain(store_path, options_path, underlying_path, symbol,
                 max_dte=DEFAULT_MAX_DTE, strike_band=DEFAULT_STRIKE_BAND,
                 start=None, end=None):
    """Import one symbol; returns a summary dict. Reads the options file one
    row group at a time, so memory stays flat."""
    import pyarrow.parquet as pq
    closes, dividends = read_underlying(underlying_path)
    connection = chain_store.connect(store_path)
    summary = {"symbol": symbol, "rows_written": 0, "sessions": 0,
               "first": None, "last": None}
    seen = set()
    try:
        chain_store.write_daily_closes(connection, symbol,
                                       {d.isoformat(): c for d, c in closes.items()})
        chain_store.write_dividends(connection, symbol,
                                    {d.isoformat(): a for d, a in dividends.items()})
        archive = pq.ParquetFile(options_path)
        for group in range(archive.num_row_groups):
            frame = archive.read_row_group(group, columns=OPTION_COLUMNS).to_pandas()
            day = pd.to_datetime(frame["date"]).dt.date
            if start:
                frame, day = frame[day >= start], day[day >= start]
            if end:
                frame = frame[day <= end]
            rows = chain_rows(frame, symbol, closes, max_dte, strike_band)
            if rows:
                chain_store.write_rows(connection, rows)
                summary["rows_written"] += len(rows)
                seen |= {row.snapshot_ms for row in rows}
    finally:
        connection.close()
    if seen:
        days = sorted(datetime.fromtimestamp(ms / 1000, tz=ET).date() for ms in seen)
        summary.update(sessions=len(days), first=days[0].isoformat(), last=days[-1].isoformat())
    return summary


def import_archive(store_path, archive_dir, symbol, max_dte=DEFAULT_MAX_DTE,
                   strike_band=DEFAULT_STRIKE_BAND, start=None, end=None):
    """Import one symbol from our own Cboe archive; returns a summary dict.
    Each snapshot's spot is stored as that day's close."""
    files = sorted((Path(archive_dir) / symbol.upper()).glob("*.csv.gz"))
    summary = {"symbol": symbol, "rows_written": 0, "sessions": 0, "first": None,
               "last": None}
    connection = chain_store.connect(store_path)
    try:
        for path in files:
            day = date.fromisoformat(path.name.split(".")[0])
            if (start and day < start) or (end and day > end):
                continue
            frame = pd.read_csv(path)
            if frame.empty:
                continue
            close = float(frame["underlying"].iloc[0])
            rows = chain_rows(frame, symbol, {day: close}, max_dte, strike_band)
            if not rows:
                continue
            chain_store.write_daily_closes(connection, symbol, {day.isoformat(): close})
            chain_store.write_rows(connection, rows)
            summary["rows_written"] += len(rows)
            summary["sessions"] += 1
            summary["first"] = summary["first"] or day.isoformat()
            summary["last"] = day.isoformat()
    finally:
        connection.close()
    return summary


def verify(path):
    """True when a file matches its pinned hash, None when none is pinned."""
    expected = EOD_HASHES.get(Path(path).name)
    if expected is None:
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest() == expected


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Import an EOD option archive into the chain store.")
    parser.add_argument("--store", required=True)
    parser.add_argument("--options", help="<SYM>_options.parquet")
    parser.add_argument("--underlying", help="<SYM>_underlying.parquet")
    parser.add_argument("--archive", help="Our Cboe archive root (tools/archive_cboe_chain.py)")
    parser.add_argument("--yahoo", action="store_true",
                        help="With --archive, also load Yahoo closes and dividends")
    parser.add_argument("--symbol", help="Root to store under (default: from the file name)")
    parser.add_argument("--max-dte", type=int, default=DEFAULT_MAX_DTE)
    parser.add_argument("--strike-band", type=float, default=DEFAULT_STRIKE_BAND)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--vix", action="store_true", help="Also load the free Cboe VIX closes")
    parser.add_argument("--verify", action="store_true",
                        help="Check both files against their pinned SHA-256 first")
    args = parser.parse_args(argv)
    if args.archive:
        if args.options or args.underlying or not args.symbol:
            parser.error("--archive takes --symbol and no --options/--underlying")
    elif not (args.options and args.underlying):
        parser.error("--options and --underlying are required unless --archive")
    if args.yahoo and not args.archive:
        parser.error("--yahoo needs --archive")
    return args


def main(argv=None, fetch_text=None):
    args = parse_args(argv)
    import backfill_chain
    fetch_text = fetch_text or backfill_chain._http_get
    if args.archive:
        symbol = args.symbol.upper()
        summary = import_archive(args.store, args.archive, symbol, args.max_dte,
                                 args.strike_band, args.start, args.end)
    else:
        symbol = args.symbol or Path(args.options).name.split("_")[0].upper()
        if args.verify:
            for path in (args.options, args.underlying):
                if verify(path) is False:
                    print(f"import_eod_chain: {path} does not match its pinned hash",
                          file=sys.stderr)
                    return 2
        summary = import_chain(args.store, args.options, args.underlying, symbol,
                               args.max_dte, args.strike_band, args.start, args.end)
    connection = chain_store.connect(args.store)
    try:
        if args.yahoo:
            summary["yahoo"] = backfill_chain.load_yahoo_closes(connection, [symbol], fetch_text)
        if args.vix:
            summary["cboe_closes"] = backfill_chain.load_cboe_closes(connection, fetch_text)
    finally:
        connection.close()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
