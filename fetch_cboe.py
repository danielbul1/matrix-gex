"""
fetch_cboe.py — מוריד שרשראות אופציות אמיתיות (מושהות ~15 דק') מ-CBOE
ומכווץ אותן ל-cboe_data.json שה-dashboard טוען.
שימוש:  python fetch_cboe.py
"""
import json, re, datetime, urllib.request, sys

# ETF/מניות (multiplier=100). אינדקסים דורשים תחילית "_" ב-CBOE (למשל _SPX).
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
RANGE = 0.25   # שומר strikes בטווח ±25% מה-spot

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
    return exp, cp, int(strike) / 1000.0

def main():
    today = datetime.date.today()
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
            exp, cp, K = p
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
                "t": cp,                         # 'C' / 'P'
                "exp": exp.isoformat(),          # תאריך תפוגה YYYY-MM-DD
                "dte": dte,
                "iv": round(o.get("iv") or 0, 4),
                "oi": int(oi),
                "vol": int(o.get("volume") or 0),
                "g": round(o.get("gamma") or 0, 7),   # gamma אמיתי מ-CBOE
                "d": round(o.get("delta") or 0, 4),
            })
        out[display_sym] = {
            "spot": round(spot, 2) if spot else None,
            "asof": d.get("last_trade_time") or str(today),
            "mult": 100,
            "opts": opts,
        }
        print(f"  {display_sym}: spot={spot}  kept={len(opts)} options")

    with open("cboe_data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote cboe_data.json  ({len(out)} symbols)")

if __name__ == "__main__":
    main()
