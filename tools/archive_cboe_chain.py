"""Archive one end-of-day option chain snapshot per symbol from Cboe.

Cboe's free delayed chains carry bid, ask, IV, greeks, open interest and
volume, but only for now: nobody keeps the history for free. This saves one
snapshot per symbol per session, so every evening adds a day to our own
history, the data a fresh Holdout needs.

Each file is `<out>/<SYMBOL>/<YYYY-MM-DD>.csv.gz`, in the end-of-day
archive's columns (tools/import_eod_chain.py --archive reads it back):
date, expiration, strike, type, bid, ask, implied_volatility, delta, gamma,
open_interest, volume, underlying.

When it may run: only on the evening of the session itself, from 16:15 ET
(when SPY and QQQ options stop trading) to midnight. Earlier, the quotes are
not closing quotes; the next morning, Cboe may already show the open
interest of the session just closed, which a close entry could not have
known. Outside that window, or when the file exists, the symbol is skipped.

Kept: expiries 0-max_dte days out, strikes within strike_band of spot,
every quote (a zero open interest still prices a wing).

Usage:
    python tools/archive_cboe_chain.py --out chain_archive [--symbols SPY,QQQ,_SPX]
        [--max-dte 45] [--strike-band 0.25]
"""
import argparse
import csv
import datetime
import gzip
import io
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_cboe  # noqa: E402

ET = ZoneInfo("America/New_York")
DEFAULT_SYMBOLS = ("SPY", "QQQ", "_SPX")
ARCHIVE_FROM = datetime.time(16, 15)  # ET; SPY/QQQ options trade until 16:15
DEFAULT_MAX_DTE = 45
DEFAULT_STRIKE_BAND = 0.25
COLUMNS = ("date", "expiration", "strike", "type", "bid", "ask", "implied_volatility",
           "delta", "gamma", "open_interest", "volume", "underlying")


def session_of(data):
    """The session a Cboe chain belongs to: the date of its last trade, ET."""
    return datetime.datetime.fromisoformat(str(data["last_trade_time"])).date()


def may_archive(session, now):
    """True on the session's own evening, from ARCHIVE_FROM ET to midnight."""
    now = now.astimezone(ET)
    return now.date() == session and now.time() >= ARCHIVE_FROM


def chain_rows(data, session, max_dte, strike_band):
    """CSV rows for one chain: expiries 0-max_dte out, strikes in the band."""
    spot = data.get("current_price") or data.get("close")
    if not spot:
        return []
    rows = []
    for option in data["options"]:
        parsed = fetch_cboe.parse_symbol(option["option"])
        if not parsed:
            continue
        _, expiry, right, strike = parsed
        if not 0 <= (expiry - session).days <= max_dte:
            continue
        if abs(strike / spot - 1) > strike_band:
            continue
        rows.append({
            "date": session.isoformat(), "expiration": expiry.isoformat(), "strike": strike,
            "type": "call" if right == "C" else "put", "bid": option.get("bid"),
            "ask": option.get("ask"), "implied_volatility": option.get("iv"),
            "delta": option.get("delta"), "gamma": option.get("gamma"),
            "open_interest": option.get("open_interest"), "volume": option.get("volume"),
            "underlying": spot,
        })
    return rows


def write_csv_gz(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    # mtime=0 keeps the bytes identical for identical rows.
    with gzip.GzipFile(path, "wb", mtime=0) as handle:
        handle.write(text.getvalue().encode("utf-8"))


def archive(out, symbols=DEFAULT_SYMBOLS, now=None, fetch=fetch_cboe.fetch,
            max_dte=DEFAULT_MAX_DTE, strike_band=DEFAULT_STRIKE_BAND):
    """Archive every symbol whose session may be archived; returns
    {symbol: what happened}."""
    now = now or datetime.datetime.now(ET)
    result = {}
    for symbol in symbols:
        name = symbol.lstrip("_")
        try:
            data = fetch(symbol)
            session = session_of(data)
        except Exception as exc:  # one symbol failing never stops the others
            result[name] = f"failed: {type(exc).__name__}: {exc}"
            continue
        path = Path(out) / name / f"{session.isoformat()}.csv.gz"
        if path.exists():
            result[name] = f"exists: {path}"
        elif not may_archive(session, now):
            result[name] = (f"skipped: session {session} may be archived only from"
                            f" {ARCHIVE_FROM:%H:%M} ET that day")
        else:
            rows = chain_rows(data, session, max_dte, strike_band)
            if rows:
                write_csv_gz(path, rows)
                result[name] = f"wrote {len(rows)} rows: {path}"
            else:
                result[name] = "skipped: no rows (no spot or no options)"
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Archive Cboe's end-of-day option chains.")
    parser.add_argument("--out", required=True, help="Archive root directory")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="Cboe symbols; indices take a leading underscore")
    parser.add_argument("--max-dte", type=int, default=DEFAULT_MAX_DTE)
    parser.add_argument("--strike-band", type=float, default=DEFAULT_STRIKE_BAND)
    return parser.parse_args(argv)


def main(argv=None, fetch=fetch_cboe.fetch, now=None):
    args = parse_args(argv)
    result = archive(args.out, tuple(args.symbols.split(",")), now, fetch,
                     args.max_dte, args.strike_band)
    print(json.dumps(result, indent=2))
    return 1 if any(v.startswith("failed") for v in result.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
