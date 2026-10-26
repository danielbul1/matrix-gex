"""
Fetch the Matrix data files: options chains (Cboe) and candles (LSE Data 3.0).

LSE moved to its Data 3.0 API in October 2026 and its option chain no longer
carries open interest, which GEX needs, so the chains come from Cboe's free
delayed snapshot (OI, IV and greeks per contract). Candles still come from
London Strategic Edge. Set LSE_API_KEY in your environment or in a .env file.

Modes:
  (no flags)        fetch options chains AND candles (backward compatible)
  --options-only    fetch only the options chains; touches cboe_data.json only
  --candles-only    fetch only the candles; touches candles_data.json only

The split modes let the scheduler refresh GEX options data more often than
candles without one mode ever overwriting the other mode's file. LSE free
plan: 60 calls a minute and one export download at a time.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import fetch_cboe


try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except Exception:
    pass


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
BASE_URL = "https://download.londonstrategicedge.com/v3"
CANDLES_TIMEFRAME = "15m"
CANDLES_LOOKBACK_DAYS = 14   # ~10 trading days of 15m bars
CANDLES_KEEP = 1000
# LSE's intraday index history has gaps; drop a symbol whose newest candle is
# older than this rather than publish a stale chart.
CANDLES_MAX_AGE_SECONDS = 4 * 24 * 3600

# Dashboard underlyings -> Cboe chain symbol (index chains take a leading "_").
OPTIONS_SYMBOLS = {
    "NDX": "_NDX",
    "SPX": "_SPX",
    "SPY": "SPY",
    "QQQ": "QQQ",
}
# Dashboard underlyings -> (LSE v3 market, LSE v3 symbol) for candles.
CANDLE_SYMBOLS = {
    "NDX": ("indices", "NDX"),
    "SPX": ("indices", "SPX"),
    "SPY": ("us_stocks", "SPY"),
    "QQQ": ("us_stocks", "QQQ"),
}

OUTPUT_OPTIONS = Path("cboe_data.json")
OUTPUT_CANDLES = Path("candles_data.json")

# LSE free plan: 60 calls a minute. This limiter only smooths request rate.
REQ_PER_MIN = 60
REQ_PER_DAY = 15000

ET = ZoneInfo("America/New_York")


# -----------------------------------------------------------------------------
# Rate limiter (simple token bucket)
# -----------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_minute: int, per_day: int):
        self.min_interval = 60.0 / per_minute
        self.day_max = per_day
        self.last_call = 0.0
        self.day_calls = 0
        self.day_started = time.time()

    def wait(self):
        now = time.time()
        # Reset daily counter if a new day started (runtime only; persist not needed).
        if now - self.day_started >= 24 * 3600:
            self.day_calls = 0
            self.day_started = now

        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
            now = time.time()

        if self.day_calls >= self.day_max:
            raise RuntimeError("daily LSE API rate limit reached")

        self.last_call = now
        self.day_calls += 1


_LIMITER = RateLimiter(REQ_PER_MIN, REQ_PER_DAY)


# -----------------------------------------------------------------------------
# API helpers
# -----------------------------------------------------------------------------
def get_api_key() -> str:
    key = os.environ.get("LSE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("LSE_API_KEY environment variable is not set")
    return key


def lse_get_text(path: str, params: dict | None = None) -> str:
    """GET an LSE v3 call and return the body as text (JSON or CSV)."""
    _LIMITER.wait()

    url = f"{BASE_URL}/{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(
        url,
        headers={
            "X-API-Key": get_api_key(),
            "Accept": "application/json, text/csv",
            "User-Agent": "Matrix GEX data pipeline (+https://github.com/danielbul1/matrix-gex)",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"LSE API error {exc.code}: {body}") from exc


def today_us() -> dt.date:
    return dt.datetime.now(ET).date()


# -----------------------------------------------------------------------------
# Options fetch (Cboe delayed chain: the only free source here with OI)
# -----------------------------------------------------------------------------
def fetch_all_options() -> dict:
    today = today_us()
    out = {}
    for display_sym, cboe_sym in OPTIONS_SYMBOLS.items():
        try:
            record = fetch_cboe.fetch_chain(cboe_sym, today)
            if not record["opts"]:
                raise RuntimeError(f"no valid options parsed for {display_sym}")
            out[display_sym] = record
            print(f"  {display_sym}: spot={record['spot']} kept={len(record['opts'])} options")
        except Exception as e:
            print(f"  {display_sym}: FAILED ({e})", file=sys.stderr)
    if not out:
        raise RuntimeError("no option data fetched from Cboe")
    return out


# -----------------------------------------------------------------------------
# Candles fetch (LSE v3 export, CSV)
# -----------------------------------------------------------------------------
def parse_candle_time(text: str) -> int:
    """LSE v3 candle times are UTC 'YYYY-MM-DD HH:MM:SS'."""
    parsed = dt.datetime.fromisoformat(text.strip().replace(" ", "T").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def fetch_candles_for_symbol(market: str, symbol: str) -> dict:
    """Fetch 15m candles from LSE v3 and convert to dashboard format."""
    today = dt.datetime.now(dt.timezone.utc).date()
    text = lse_get_text("export", {
        "market": market,
        "symbol": symbol,
        "timeframe": CANDLES_TIMEFRAME,
        "data": "candles",
        "format": "csv",
        "from": (today - dt.timedelta(days=CANDLES_LOOKBACK_DAYS)).isoformat(),
        "to": today.isoformat(),
    })

    candles = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            values = {name: float(row[name]) for name in ("open", "high", "low", "close")}
            ts = parse_candle_time(row["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(v > 0 for v in values.values()):
            continue
        candles.append({"time": ts, **{name: round(value, 4) for name, value in values.items()}})

    candles.sort(key=lambda c: c["time"])
    candles = candles[-CANDLES_KEEP:]
    if not candles:
        raise RuntimeError(f"no valid candles for {symbol}")
    if candles[-1]["time"] < time.time() - CANDLES_MAX_AGE_SECONDS:
        raise RuntimeError(f"newest {symbol} candle is stale ({candles[-1]['time']})")

    return {
        "source": "London Strategic Edge",
        "ticker": symbol,
        "interval": CANDLES_TIMEFRAME,
        "exchangeTimezone": "America/New_York",
        "asof": dt.datetime.fromtimestamp(candles[-1]["time"], dt.timezone.utc).isoformat(),
        "candles": candles,
    }


def fetch_all_candles() -> dict:
    out = {}
    for display_sym, (market, lse_sym) in CANDLE_SYMBOLS.items():
        try:
            out[display_sym] = fetch_candles_for_symbol(market, lse_sym)
            print(f"  {display_sym}: {len(out[display_sym]['candles'])} candles")
        except Exception as e:
            print(f"  {display_sym}: FAILED ({e})", file=sys.stderr)
    if not out:
        raise RuntimeError("no candle data fetched from LSE")
    return out


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fetch options chains (Cboe) and/or candles (London Strategic Edge).")
    parser.add_argument("--options-only", action="store_true",
                        help="fetch only the options chains (writes cboe_data.json only)")
    parser.add_argument("--candles-only", action="store_true",
                        help="fetch only the candles (writes candles_data.json only)")
    args = parser.parse_args()
    if args.options_only and args.candles_only:
        parser.error("--options-only and --candles-only are mutually exclusive")

    # Default (no flags) keeps backward-compatible behavior: fetch both.
    want_options = not args.candles_only
    want_candles = not args.options_only

    if want_options:
        print("Fetching options from Cboe...")
        options_data = fetch_all_options()
        OUTPUT_OPTIONS.write_text(json.dumps(options_data, separators=(",", ":")), encoding="utf-8")
        print(f"wrote {OUTPUT_OPTIONS} ({len(options_data)} symbols)")

    if want_candles:
        print("Fetching candles from LSE...")
        candles_data = fetch_all_candles()
        OUTPUT_CANDLES.write_text(json.dumps(candles_data, separators=(",", ":")), encoding="utf-8")
        print(f"wrote {OUTPUT_CANDLES} ({len(candles_data)} symbols)")


if __name__ == "__main__":
    main()
