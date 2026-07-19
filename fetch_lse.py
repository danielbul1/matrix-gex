"""
Fetch options chains and candles from London Strategic Edge (LSE).

This is the PRIMARY data source for Matrix. It does NOT fall back to CBOE or Yahoo.
Set LSE_API_KEY in your environment or in a .env file.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except Exception:
    pass


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
BASE_URL = "https://api.londonstrategicedge.com"
OPTIONS_ENDPOINT = "x_options_snapshot"
CANDLES_ENDPOINT = "x_candles_15m"  # LSE does not expose 5m candles

# Dashboard supports these underlyings.  NDX/SPX must be queried by their
# common underlying symbol; the option ticker itself carries the real root.
SYMBOLS = {
    "NDX": "NDX",      # NAS100
    "SPX": "SPX",      # SPX500
    "SPY": "SPY",
    "QQQ": "QQQ",
}

OPTIONS_SYMBOLS = {k: v for k, v in SYMBOLS.items() if not v.endswith("=F")}
CANDLE_SYMBOLS = SYMBOLS

OUTPUT_OPTIONS = Path("cboe_data.json")
OUTPUT_CANDLES = Path("candles_data.json")

# LSE Registered plan limits from /keys/plans (adjust if your plan differs).
# We stay well under these: one full refresh is ~10 requests.
REQ_PER_MIN = 60
REQ_PER_DAY = 15000

ET = ZoneInfo("America/New_York")

# Parse OCC-style option ticker: ROOT + YYMMDD + C/P + STRIKE*1000
OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


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


def lse_request(path: str, params: dict | None = None) -> dict | list:
    """Make a GET request to LSE and return JSON."""
    _LIMITER.wait()

    url = f"{BASE_URL}/{path}"
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}"

    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": get_api_key(),
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"LSE API error {exc.code}: {body}") from exc


# -----------------------------------------------------------------------------
# Options parsing
# -----------------------------------------------------------------------------
def parse_option_ticker(ticker: str):
    """Return (root, expiry_date, call_put, strike) or None."""
    if not ticker:
        return None
    m = OCC_RE.match(ticker.upper())
    if not m:
        return None
    root, date_s, cp, strike_s = m.groups()
    exp = dt.date(2000 + int(date_s[:2]), int(date_s[2:4]), int(date_s[4:6]))
    strike = int(strike_s) / 1000.0
    return root, exp, cp, strike


def today_us() -> dt.date:
    return dt.datetime.now(ET).date()


# -----------------------------------------------------------------------------
# Options fetch
# -----------------------------------------------------------------------------
def fetch_options_for_symbol(symbol: str) -> dict:
    """Fetch LSE options snapshot and convert to dashboard format."""
    today = today_us()
    params = {
        "underlying": f"eq.{symbol}",
        "order": "ticker.asc",
        "limit": "30000",
        "select": "ticker,underlying,last_price,volume,open_interest,iv,delta,gamma,theta,vega,underlying_price",
    }
    rows = lse_request(OPTIONS_ENDPOINT, params)

    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected LSE options response type: {type(rows)}")

    # LSE sometimes splits an underlying across multiple option roots
    # (e.g. SPX/SPXW, SPY/SPY7).  We keep every contract and let downstream
    # code aggregate by strike; the 'root' field records the real root.
    opts = []
    spot = None
    asof_ts = None

    for row in rows:
        ticker = (row.get("ticker") or "").strip()
        parsed = parse_option_ticker(ticker)
        if not parsed:
            continue
        root, exp, cp, strike = parsed

        if exp < today:
            continue

        oi = int(row.get("open_interest") or 0)
        if oi <= 0:
            continue

        iv_raw = row.get("iv") or 0
        # LSE may return IV as a percentage (e.g. 13.5) or decimal (0.135).
        # Normalize to decimal used by the dashboard.
        iv = float(iv_raw)
        if iv > 1:
            iv = iv / 100.0

        gamma = float(row.get("gamma") or 0)
        delta = float(row.get("delta") or 0)
        volume = int(row.get("volume") or 0)

        # Spot from LSE; use the first valid value we see.
        if spot is None:
            spot = float(row.get("underlying_price") or 0) or None

        # Use the row's last trade time if present, otherwise build one.
        if asof_ts is None and row.get("last_price") is not None:
            asof_ts = dt.datetime.now(dt.timezone.utc).isoformat()

        dte = max(0, (exp - today).days)

        opts.append({
            "k": round(strike, 2),
            "t": cp,                         # "C" / "P"
            "root": root,                    # SPX, SPXW, SPY, SPY7, etc.
            "exp": exp.isoformat(),          # YYYY-MM-DD
            "dte": dte,
            "iv": round(iv, 4),
            "oi": oi,
            "vol": volume,
            "g": round(gamma, 7),
            "d": round(delta, 4),
        })

    if not opts:
        raise RuntimeError(f"no valid options parsed for {symbol}")

    return {
        "spot": round(spot, 2) if spot else None,
        "asof": asof_ts or dt.datetime.now(dt.timezone.utc).isoformat(),
        "mult": 100,
        "opts": opts,
    }


def fetch_all_options() -> dict:
    out = {}
    for display_sym, lse_sym in OPTIONS_SYMBOLS.items():
        try:
            out[display_sym] = fetch_options_for_symbol(lse_sym)
            print(f"  {display_sym}: spot={out[display_sym]['spot']} kept={len(out[display_sym]['opts'])} options")
        except Exception as e:
            print(f"  {display_sym}: FAILED ({e})", file=sys.stderr)
    if not out:
        raise RuntimeError("no option data fetched from LSE")
    return out


# -----------------------------------------------------------------------------
# Candles fetch
# -----------------------------------------------------------------------------
def fetch_candles_for_symbol(symbol: str) -> dict:
    """Fetch 15m candles from LSE and convert to dashboard format."""
    params = {
        "symbol": f"eq.{symbol}",
        "order": "timestamp.desc",
        "limit": "1000",  # ~10 trading days of 15m bars
    }
    rows = lse_request(CANDLES_ENDPOINT, params)

    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected LSE candles response type: {type(rows)}")

    candles = []
    for row in rows:
        ts = row.get("timestamp")
        if not ts:
            continue
        try:
            values = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if not all(v > 0 for v in values.values()):
            continue
        candles.append({
            "time": int(ts),
            **{name: round(value, 4) for name, value in values.items()},
        })

    candles.sort(key=lambda c: c["time"])
    if not candles:
        raise RuntimeError(f"no valid candles for {symbol}")

    return {
        "source": "London Strategic Edge",
        "ticker": symbol,
        "interval": "15m",
        "exchangeTimezone": "America/New_York",
        "asof": dt.datetime.fromtimestamp(candles[-1]["time"], dt.timezone.utc).isoformat(),
        "candles": candles,
    }


def fetch_all_candles() -> dict:
    out = {}
    for display_sym, lse_sym in CANDLE_SYMBOLS.items():
        try:
            out[display_sym] = fetch_candles_for_symbol(lse_sym)
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
    print("Fetching options from LSE...")
    options_data = fetch_all_options()
    OUTPUT_OPTIONS.write_text(json.dumps(options_data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUTPUT_OPTIONS} ({len(options_data)} symbols)")

    print("Fetching candles from LSE...")
    candles_data = fetch_all_candles()
    OUTPUT_CANDLES.write_text(json.dumps(candles_data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUTPUT_CANDLES} ({len(candles_data)} symbols)")


if __name__ == "__main__":
    main()
