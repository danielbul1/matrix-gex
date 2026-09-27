"""Backfill the chain store from ThetaData, or probe what the subscription has.

Backfill: for every weekday in [start, end] and every root (SPX, SPXW), pull
the 0-max_dte expiries' first-order greeks at the grid times -- bid, ask, IV
and delta per contract -- plus the day's open interest, normalize them into
the chain store, and checkpoint the session. The free Cboe VIX and SPX daily
closes are loaded first.

Normalization:
- SOD Open Interest for session T is positions at the close of T-1. Two
  vendor stampings qualify: a report stamped on T before the open (ThetaData
  documents ~06:30 ET), or a report stamped at/after the close of an earlier
  day (the latest such, reaching back past weekends and holidays). Anything
  else is refused: a report stamped after T's open is look-ahead, an earlier
  day's morning report holds positions at the close of T-2, and a stamp with
  no time of day cannot be told apart and fails the session. Each contract
  takes its own latest qualifying stamp; a contract missing from the report
  has zero open interest.
- Grid rows are ThetaData interval rows, which carry the last quote at the
  row's timestamp, so the 10:00 row never holds later quotes.
- The underlying level at each snapshot comes from put-call parity under the
  canonical greeks engine's model (rate DEFAULT_RATE, no dividends):
  S = C - P + K e^(-rT), the median over the PARITY_PAIRS strikes nearest the
  money on the nearest unexpired expiry. The level at a session's last
  snapshot is cross-checked against the Cboe SPX close.

Resilience: a session is checkpointed once all its rows are written, and a
rerun skips checkpointed sessions. A session with no vendor data (a holiday,
or a date the tier does not serve) is reported as empty and not checkpointed,
so a rerun asks again. At most `concurrency` vendor requests are
in flight at once (4 on the Standard tier). A failed session is reported and
left unchecked for the next run.

Probe (--probe) answers the day-one questions before any bulk download: the
earliest SPXW date (the earliest listed, confirmed with a data request, plus
whether 2018-01 data comes back, since the listings may not follow the
tier), whether open interest is stamped at the start or the end of the day,
and whether an SPX underlying price is supplied before 2022.

The API key is read from THETADATA_API_KEY and never written or printed.
Requires Python 3.12+ (the `thetadata` library's floor).

Usage:
    python tools/backfill_chain.py --store PATH --start DATE --end DATE
        [--roots SPX,SPXW] [--times 09:30,10:00,...] [--max-dte 10]
        [--concurrency 4] [--skip-cboe]
    python tools/backfill_chain.py --probe [--probe-date DATE]
"""
import argparse
import csv
import io
import json
import logging
import math
import os
import statistics
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
from tripity_experiment import chain_store
from tripity_experiment import matrix_gex

ET = matrix_gex.ET
API_KEY_ENV = "THETADATA_API_KEY"

MARKET_OPEN = (9, 30)  # ET
MARKET_CLOSE = (16, 0)  # ET
DEFAULT_TIMES = tuple((9 + (30 + 30 * i) // 60, (30 + 30 * i) % 60) for i in range(14))
# Vendor bar sizes, coarsest first; the grid is pulled at the coarsest one
# that lands on every grid time.
VENDOR_INTERVALS = ((60, "1h"), (30, "30m"), (15, "15m"), (10, "10m"),
                    (5, "5m"), (1, "1m"))
PARITY_PAIRS = 3  # strikes nearest the money used for the parity level
PARITY_CLOSE_TOLERANCE = 0.005  # relative gap to the Cboe close worth reporting
OI_LOOKBACK_DAYS = 7  # how far back to look for a close-of-day OI report
PROBE_PRE_2022_DATE = date(2021, 6, 14)  # a Monday with an SPX monthly that week
PROBE_DEV_START = date(2018, 1, 2)  # first session of the Dev Period

CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
CBOE_CLOSE_COLUMNS = {"VIX": "CLOSE", "SPX": "SPX"}

CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_checkpoint (
    session_date TEXT NOT NULL,
    root TEXT NOT NULL,
    rows INTEGER NOT NULL,
    PRIMARY KEY (session_date, root)
);
"""


@dataclass(frozen=True)
class BackfillConfig:
    roots: tuple = ("SPX", "SPXW")
    times: tuple = DEFAULT_TIMES  # ((hour, minute), ...) ET
    max_dte: int = 10
    concurrency: int = 4


# ---------------------------------------------------------------------------
# Data clients
# ---------------------------------------------------------------------------
class ThetaDataClient:
    """The production client: the `thetadata` library, direct connection (no
    Theta Terminal), pandas output. Every method returns the vendor's frame,
    or an empty frame when the vendor has no data."""

    def __init__(self, api_key):
        from thetadata import ThetaClient
        from thetadata.errors import NoDataFoundError
        # The library logs the full auth response at INFO and the server's
        # rejection at ERROR; its errors reach us as exceptions, redacted.
        logging.getLogger("thetadata").setLevel(logging.CRITICAL + 1)
        self._no_data = NoDataFoundError
        self._client = ThetaClient(api_key=api_key, dataframe_type="pandas")

    def _call(self, method, **params):
        try:
            return getattr(self._client, method)(**params)
        except self._no_data:
            return pd.DataFrame()

    def list_expirations(self, root):
        return self._call("option_list_expirations", symbol=root)

    def list_dates(self, root, expiration):
        return self._call("option_list_dates", request_type="quote", symbol=root,
                          expiration=expiration)

    def greeks(self, root, expiration, day, interval):
        return self._call("option_history_greeks_first_order", symbol=root,
                          expiration=expiration, date=day, interval=interval)

    def open_interest(self, root, day, max_dte):
        return self._call("option_history_open_interest", symbol=root,
                          expiration="*", date=day, max_dte=max_dte)


class _ConcurrencyCappedClient:
    """Wraps a client so at most `limit` requests are in flight at once."""

    def __init__(self, client, limit):
        self._client = client
        self._slots = threading.BoundedSemaphore(limit)

    def __getattr__(self, name):
        method = getattr(self._client, name)

        def call(*args, **kwargs):
            with self._slots:
                return method(*args, **kwargs)
        return call


# ---------------------------------------------------------------------------
# Vendor frame helpers
# ---------------------------------------------------------------------------
def vendor_interval(times):
    """The coarsest vendor bar size that lands on every grid time."""
    offsets = [(h * 60 + m) - (MARKET_OPEN[0] * 60 + MARKET_OPEN[1]) for h, m in times]
    step = math.gcd(*offsets) if any(offsets) else 60
    return next(name for minutes, name in VENDOR_INTERVALS if step % minutes == 0)


def _to_et(value):
    stamp = pd.Timestamp(value)
    return (stamp.tz_localize(ET) if stamp.tzinfo is None else stamp.tz_convert(ET)).to_pydatetime()


def _dates(frame, column):
    if frame.empty:
        return []
    values = frame[column] if column in frame else frame.iloc[:, 0]
    return sorted({pd.Timestamp(v).date() for v in values})


def _number(value):
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) else value


def _right(value):
    return str(value).strip()[0].upper()


def _chain_row(root, record, snapshot_ms):
    """A vendor greeks record as a chain-store row, before OI and the
    underlying level are attached."""
    iv = _number(record.get("implied_vol"))
    return chain_store.ChainRow(
        root=root, expiry=pd.Timestamp(record["expiration"]).date().isoformat(),
        strike=float(record["strike"]), right=_right(record["right"]),
        snapshot_ms=snapshot_ms, bid=_number(record.get("bid")),
        ask=_number(record.get("ask")), iv=iv if iv else None, sod_oi=None,
        vendor_delta=_number(record.get("delta")),
        vendor_gamma=_number(record.get("gamma")), underlying=None)


def _contract_of(row):
    """The key an open-interest report is looked up by."""
    return (row.root, row.expiry, row.strike, row.right)


def _et_ms(day, hour, minute):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


# ---------------------------------------------------------------------------
# SOD Open Interest
# ---------------------------------------------------------------------------
def _is_date_only(stamp):
    return (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) == (0, 0, 0, 0)


def _holds_previous_close(stamp, session):
    """Whether a report stamped at `stamp` holds positions at the close of
    the session before `session`."""
    if stamp.date() == session:
        return (stamp.hour, stamp.minute) < MARKET_OPEN
    return stamp.date() < session and (stamp.hour, stamp.minute) >= MARKET_CLOSE


def sod_open_interest(client, root, session, max_dte):
    """{contract key: SOD Open Interest} for the session, or None when no
    qualifying report exists. Walks back from the session to the first day
    with a qualifying report."""
    for back in range(OI_LOOKBACK_DAYS + 1):
        day = session - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        latest = {}  # contract key -> (stamp, open interest)
        for record in client.open_interest(root, day, max_dte + back).to_dict("records"):
            stamp = _to_et(record["timestamp"])
            if _is_date_only(stamp):
                raise ValueError(f"{root} open interest for {day} has no time of day;"
                                 " cannot tell start-of-day from end-of-day stamping")
            if not _holds_previous_close(stamp, session):
                continue
            key = _contract_of(_chain_row(root, record, 0))
            if key not in latest or stamp > latest[key][0]:
                latest[key] = (stamp, float(record["open_interest"]))
        if latest:
            return {key: oi for key, (_, oi) in latest.items()}
    return None


# ---------------------------------------------------------------------------
# Underlying from put-call parity
# ---------------------------------------------------------------------------
def _mid(row):
    if row.bid is None or row.ask is None or row.bid <= 0 or row.ask < row.bid:
        return None
    return (row.bid + row.ask) / 2


def parity_level(rows):
    """Underlying level implied by one snapshot's quotes, or None."""
    by_expiry = {}
    for row in rows:
        by_expiry.setdefault((row.expiry, row.root), {}).setdefault(row.strike, {})[row.right] = row
    candidates = []
    for (expiry, root), strikes in by_expiry.items():
        T = matrix_gex.years_to_expiry(expiry, root, rows[0].snapshot_ms)
        if T <= matrix_gex.MIN_T:
            continue  # expired or expiring now: no parity left in the quotes
        pairs = []
        for strike, legs in strikes.items():
            call, put = legs.get("C"), legs.get("P")
            call_mid, put_mid = (_mid(call) if call else None), (_mid(put) if put else None)
            if call_mid is not None and put_mid is not None:
                pairs.append((abs(call_mid - put_mid),
                              call_mid - put_mid + strike * math.exp(-matrix_gex.DEFAULT_RATE * T)))
        if pairs:
            candidates.append((T, pairs))
    if not candidates:
        return None
    _, pairs = min(candidates, key=lambda c: c[0])
    return statistics.median(level for _, level in sorted(pairs)[:PARITY_PAIRS])


# ---------------------------------------------------------------------------
# One session
# ---------------------------------------------------------------------------
def fetch_session(client, session, expirations, config):
    """[ChainRow, ...] for one session across every configured root."""
    interval = vendor_interval(config.times)
    grid = set(config.times)
    rows = []
    for root in config.roots:
        expiries = [e for e in expirations[root] if 0 <= (e - session).days <= config.max_dte]
        root_rows = []
        for expiry in expiries:
            frame = client.greeks(root, expiry, session, interval)
            for record in frame.to_dict("records"):
                stamp = _to_et(record["timestamp"])
                if stamp.date() == session and (stamp.hour, stamp.minute) in grid:
                    root_rows.append(_chain_row(root, record,
                                                _et_ms(session, stamp.hour, stamp.minute)))
        if root_rows:
            oi = sod_open_interest(client, root, session, config.max_dte)
            rows += root_rows if oi is None else [
                replace(row, sod_oi=oi.get(_contract_of(row), 0.0)) for row in root_rows]
    by_snapshot = {}
    for row in rows:
        by_snapshot.setdefault(row.snapshot_ms, []).append(row)
    levels = {ms: parity_level(snapshot) for ms, snapshot in by_snapshot.items()}
    return [replace(row, underlying=levels[row.snapshot_ms]) for row in rows]


# ---------------------------------------------------------------------------
# Checkpoints and the run
# ---------------------------------------------------------------------------
def _done_sessions(connection, roots):
    done = {}
    for session_date, root in connection.execute(
            "SELECT session_date, root FROM backfill_checkpoint"):
        done.setdefault(session_date, set()).add(root)
    return {date.fromisoformat(d) for d, got in done.items() if set(roots) <= got}


def _checkpoint(connection, session, rows, roots):
    counts = {root: 0 for root in roots}
    for row in rows:
        counts[row.root] += 1
    connection.executemany(
        "INSERT OR REPLACE INTO backfill_checkpoint (session_date, root, rows)"
        " VALUES (?, ?, ?)", [(session.isoformat(), root, n) for root, n in counts.items()])
    connection.commit()


def _parity_mismatch(session, rows, closes):
    close = closes.get(session.isoformat())
    if close is None or not rows:
        return None
    last = max(row.snapshot_ms for row in rows)
    level = next((r.underlying for r in rows if r.snapshot_ms == last and r.underlying), None)
    if level is None or abs(level - close) / close <= PARITY_CLOSE_TOLERANCE:
        return None
    return {"session": session.isoformat(), "parity_level": round(level, 4),
            "cboe_close": close}


def run_backfill(client, store_path, sessions, config, redact=str):
    """Backfill `sessions` into the store at store_path; returns the summary.
    redact: applied to every error message before it enters the summary."""
    client = _ConcurrencyCappedClient(client, config.concurrency)
    connection = chain_store.connect(store_path)
    try:
        connection.executescript(CHECKPOINT_SCHEMA)
        done = _done_sessions(connection, config.roots)
        todo = sorted(set(sessions) - done)
        closes = chain_store.read_daily_closes(connection, "SPX")
        summary = {"sessions": len(set(sessions)), "already_done": len(set(sessions) & done),
                   "completed": [], "empty": [], "failed": [], "rows_written": 0,
                   "parity_mismatches": []}
        if not todo:
            return summary
        expirations = {root: _dates(client.list_expirations(root), "expiration")
                       for root in config.roots}
        with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
            futures = {pool.submit(fetch_session, client, session, expirations, config): session
                       for session in todo}
            for future in as_completed(futures):
                session = futures[future]
                try:
                    rows = future.result()
                except Exception as exc:  # reported; the session stays unchecked
                    summary["failed"].append({
                        "session": session.isoformat(),
                        "error": redact(f"{type(exc).__name__}: {exc}")})
                    continue
                if not rows:  # a holiday, or a date the tier does not serve
                    summary["empty"].append(session.isoformat())
                    continue
                chain_store.write_rows(connection, rows)
                _checkpoint(connection, session, rows, config.roots)
                summary["completed"].append(session.isoformat())
                summary["rows_written"] += len(rows)
                mismatch = _parity_mismatch(session, rows, closes)
                if mismatch:
                    summary["parity_mismatches"].append(mismatch)
    finally:
        connection.close()
    summary["completed"].sort()
    summary["empty"].sort()
    summary["failed"].sort(key=lambda f: f["session"])
    summary["parity_mismatches"].sort(key=lambda m: m["session"])
    return summary


# ---------------------------------------------------------------------------
# Cboe daily closes
# ---------------------------------------------------------------------------
def _http_get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def load_cboe_closes(connection, fetch_text=_http_get):
    """Load the free Cboe VIX and SPX daily closes; returns rows per symbol."""
    counts = {}
    for symbol, column in CBOE_CLOSE_COLUMNS.items():
        reader = csv.DictReader(io.StringIO(fetch_text(CBOE_URL.format(symbol))))
        closes = {datetime.strptime(r["DATE"], "%m/%d/%Y").date().isoformat(): float(r[column])
                  for r in reader if r.get(column)}
        counts[symbol] = chain_store.write_daily_closes(connection, symbol, closes)
    return counts


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------
def _stamping(frame, day):
    if frame.empty:
        return "unknown (no open interest reported)"
    stamp = _to_et(frame["timestamp"].iloc[0])
    if _is_date_only(stamp):
        return "unknown (date only, no time of day)"
    if stamp.date() < day or (stamp.hour, stamp.minute) < MARKET_OPEN:
        return "start of day"
    if (stamp.hour, stamp.minute) >= MARKET_CLOSE:
        return "end of day"
    return f"intraday ({stamp:%H:%M} ET)"


def run_probe(client, probe_date):
    """The three day-one answers, as a dict."""
    interval = vendor_interval(DEFAULT_TIMES)
    spxw_expirations = _dates(client.list_expirations("SPXW"), "expiration")
    earliest, earliest_served = None, False
    if spxw_expirations:
        quote_dates = _dates(client.list_dates("SPXW", spxw_expirations[0]), "date")
        earliest = (quote_dates or spxw_expirations)[0]
        earliest_served = not client.greeks("SPXW", spxw_expirations[0], earliest,
                                            interval).empty
    dev_expiry = next((e for e in spxw_expirations if e > PROBE_DEV_START), None)
    dev_start_served = dev_expiry is not None and not client.greeks(
        "SPXW", dev_expiry, PROBE_DEV_START, interval).empty

    stamped = _stamping(client.open_interest("SPXW", probe_date, 10), probe_date)

    spx_expiry = next((e for e in _dates(client.list_expirations("SPX"), "expiration")
                       if e > PROBE_PRE_2022_DATE), None)
    supplied = False
    if spx_expiry is not None:
        frame = client.greeks("SPX", spx_expiry, PROBE_PRE_2022_DATE, interval)
        if not frame.empty and "underlying_price" in frame:
            prices = pd.to_numeric(frame["underlying_price"], errors="coerce")
            supplied = bool((prices > 0).any())
    return {"earliest_spxw_date": earliest and earliest.isoformat(),
            "earliest_spxw_date_served": earliest_served,
            "spxw_dev_start_served": dev_start_served,
            "open_interest_stamped": stamped,
            "spx_underlying_before_2022": supplied}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_times(text):
    return tuple(tuple(int(part) for part in t.split(":")) for t in text.split(","))


def _weekdays(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


def _last_weekday_before(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill the chain store from ThetaData, or probe the subscription.")
    parser.add_argument("--probe", action="store_true",
                        help="Answer the day-one questions and exit")
    parser.add_argument("--probe-date", type=date.fromisoformat,
                        help="Session whose open-interest stamp the probe inspects"
                             " (default: the last weekday)")
    parser.add_argument("--store", help="Path to the chain store SQLite file")
    parser.add_argument("--start", type=date.fromisoformat,
                        help="First session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="Last session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--roots", default=",".join(BackfillConfig.roots))
    parser.add_argument("--times", type=_parse_times, default=DEFAULT_TIMES,
                        help="Grid times, ET, e.g. 10:00,15:45 (default: every 30"
                             " minutes 09:30-16:00)")
    parser.add_argument("--max-dte", type=int, default=BackfillConfig.max_dte)
    parser.add_argument("--concurrency", type=int, default=BackfillConfig.concurrency,
                        help="Most vendor requests in flight (Standard tier: 4)")
    parser.add_argument("--skip-cboe", action="store_true",
                        help="Do not refresh the Cboe VIX and SPX closes")
    args = parser.parse_args(argv)
    if not args.probe and not (args.store and args.start and args.end):
        parser.error("--store, --start and --end are required unless --probe")
    return args


def main(argv=None, client_factory=ThetaDataClient, fetch_text=_http_get):
    if sys.version_info < (3, 12):
        print("backfill_chain: the thetadata library needs Python 3.12+", file=sys.stderr)
        return 2
    args = parse_args(argv)
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"backfill_chain: set {API_KEY_ENV} to your ThetaData API key", file=sys.stderr)
        return 2

    def redact(text):
        return str(text).replace(api_key, "***")

    try:
        client = client_factory(api_key)
    except Exception as exc:  # e.g. the vendor rejected the key
        print(f"backfill_chain: could not connect: {redact(exc)}", file=sys.stderr)
        return 2
    try:
        if args.probe:
            probe = run_probe(client, args.probe_date or _last_weekday_before(
                datetime.now(ET).date()))
            served = "yes" if probe["earliest_spxw_date_served"] else "no"
            print(f"Earliest SPXW date: {probe['earliest_spxw_date'] or 'none listed'}"
                  f" (data returned: {served})")
            print(f"SPXW data on {PROBE_DEV_START}: "
                  f"{'yes' if probe['spxw_dev_start_served'] else 'no'}")
            print(f"Open interest stamped at: {probe['open_interest_stamped']}")
            print("SPX underlying price before 2022: "
                  f"{'yes' if probe['spx_underlying_before_2022'] else 'no'}")
            return 0
        config = BackfillConfig(roots=tuple(args.roots.split(",")), times=args.times,
                                max_dte=args.max_dte, concurrency=args.concurrency)
        if not args.skip_cboe:
            connection = chain_store.connect(args.store)
            try:
                load_cboe_closes(connection, fetch_text)
            finally:
                connection.close()
        summary = run_backfill(client, args.store, _weekdays(args.start, args.end),
                               config, redact=redact)
    except Exception as exc:  # vendor errors included; never an unredacted traceback
        print(f"backfill_chain: {type(exc).__name__}: {redact(exc)}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
