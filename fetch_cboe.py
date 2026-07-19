"""
Fetch delayed CBOE option chains and compact them into cboe_data.json.

Usage: python fetch_cboe.py
"""
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


# ETFs and equities use multiplier=100. CBOE index symbols require a leading "_".
SYMBOLS = [
    ("_NDX", "NDX"),
    ("_SPX", "SPX"),
    ("SPY", "SPY"),
    ("QQQ", "QQQ"),
    ("IWM", "IWM"),
    ("AAPL", "AAPL"),
    ("NVDA", "NVDA"),
    ("TSLA", "TSLA"),
]
URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
OCC = re.compile(r"([A-Z]+)(\d{6})([CP])(\d{8})")
RANGE = 0.25  # keep strikes within +/-25% of spot
OPEN_DATA_PATH = Path("cboe_open_data.json")
ET = ZoneInfo("America/New_York")
OPEN_CAPTURE_START = datetime.time(9, 30)
OPEN_CAPTURE_END = datetime.time(9, 45)
MARKET_CAPTURE_END = datetime.time(16, 15)


def fetch(sym):
    url = URL.format(sym)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["data"]


def parse_symbol(s):
    m = OCC.match(s)
    if not m:
        return None
    root, dt, cp, strike = m.groups()
    exp = datetime.date(2000 + int(dt[:2]), int(dt[2:4]), int(dt[4:6]))
    return root, exp, cp, int(strike) / 1000.0


def main():
    # Use US Eastern date for expiry filtering, not the machine's local date.
    today = datetime.datetime.now(ET).date()
    out = {}
    for cboe_sym, display_sym in SYMBOLS:
        try:
            d = fetch(cboe_sym)
        except Exception as e:
            print(f"  {display_sym}: FAILED ({e})", file=sys.stderr)
            continue
        spot = d.get("current_price") or d.get("close")
        opts = []
        for o in d["options"]:
            p = parse_symbol(o["option"])
            if not p:
                continue
            root, exp, cp, K = p
            if exp < today:
                continue
            if spot and abs(K - spot) / spot > RANGE:
                continue
            oi = o.get("open_interest") or 0
            if oi <= 0:
                continue
            dte = max(0, (exp - today).days)
            opts.append({
                "k": round(K, 2),
                "t": cp,                         # "C" / "P"
                "root": root,                    # SPX/SPXW, NDX/NDXP, etc.
                "exp": exp.isoformat(),          # expiration date YYYY-MM-DD
                "dte": dte,
                "iv": round(o.get("iv") or 0, 4),
                "oi": int(oi),
                "vol": int(o.get("volume") or 0),
                "g": round(o.get("gamma") or 0, 7),   # gamma from CBOE
                "d": round(o.get("delta") or 0, 4),
            })
        out[display_sym] = {
            "spot": round(spot, 2) if spot else None,
            "asof": d.get("last_trade_time") or str(today),
            "mult": 100,
            "opts": opts,
        }
        print(f"  {display_sym}: spot={spot}  kept={len(opts)} options")

    if not out:
        # Mirror fetch_lse.py: never clobber cboe_data.json with an empty
        # payload when every symbol fetch failed.
        print("ERROR: no option data fetched from CBOE; keeping existing cboe_data.json", file=sys.stderr)
        sys.exit(1)

    with open("cboe_data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote cboe_data.json  ({len(out)} symbols)")

    now_et = datetime.datetime.now(ET)
    session_date = now_et.date().isoformat()
    existing_session = None
    if OPEN_DATA_PATH.exists():
        try:
            existing_session = json.loads(OPEN_DATA_PATH.read_text()).get("session_date")
        except Exception:
            existing_session = None
    is_open_window = now_et.weekday() < 5 and OPEN_CAPTURE_START <= now_et.time() <= OPEN_CAPTURE_END
    is_first_available_window = now_et.weekday() < 5 and OPEN_CAPTURE_END < now_et.time() <= MARKET_CAPTURE_END
    if is_open_window or (is_first_available_window and existing_session != session_date):
        if existing_session != session_date:
            OPEN_DATA_PATH.write_text(
                json.dumps({"session_date": session_date, "captured_at": now_et.isoformat(),
                            "capture_kind": "open" if is_open_window else "first_available", "data": out}, separators=(",", ":"))
            )
            print(f"wrote {OPEN_DATA_PATH} for session {session_date}")
        else:
            print(f"kept existing {OPEN_DATA_PATH} for session {session_date}")
    else:
        print(f"did not write {OPEN_DATA_PATH}; outside opening capture window")


if __name__ == "__main__":
    main()
