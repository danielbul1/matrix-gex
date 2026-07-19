"""Fetch compact 5-minute OHLC candles for the dashboard data branch.

DEPRECATED (2026-07-19): part of the retired local collector pipeline,
superseded by the GitHub Actions pipeline (.github/workflows/update-cboe.yml),
which fetches 15m candles from LSE via fetch_lse.py, and by the Railway
backend service at https://api.trytripity.site. Kept for reference only.
"""

import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path


SYMBOLS = {
    "NDX": "^NDX",
    "SPX": "^SPX",
    "SPY": "SPY",
    "QQQ": "QQQ",
    "IWM": "IWM",
    "AAPL": "AAPL",
    "NVDA": "NVDA",
    "TSLA": "TSLA",
}
OUTPUT = Path("candles_data.json")


def fetch_chart(ticker):
    encoded = urllib.parse.quote(ticker, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=5m&range=1d"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 Matrix-GEX/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    fields = {name: quote.get(name) or [] for name in ("open", "high", "low", "close")}
    candles = []
    for index, timestamp in enumerate(timestamps):
        try:
            values = {name: float(fields[name][index]) for name in fields}
        except (IndexError, TypeError, ValueError):
            continue
        if not all(value > 0 for value in values.values()):
            continue
        candles.append({"time": int(timestamp), **{name: round(value, 4) for name, value in values.items()}})
    meta = result.get("meta") or {}
    return candles, meta


def main():
    output = {}
    for symbol, ticker in SYMBOLS.items():
        try:
            candles, meta = fetch_chart(ticker)
            if not candles:
                raise RuntimeError("no valid OHLC rows")
            output[symbol] = {
                "source": "Yahoo Finance",
                "ticker": ticker,
                "interval": "5m",
                "exchangeTimezone": meta.get("exchangeTimezoneName"),
                "asof": dt.datetime.fromtimestamp(candles[-1]["time"], dt.timezone.utc).isoformat(),
                "candles": candles,
            }
            print(f"  {symbol}: {len(candles)} candles")
        except Exception as error:
            print(f"  {symbol}: FAILED ({error})")
    if not output:
        raise RuntimeError("no candle data fetched")
    OUTPUT.write_text(json.dumps(output, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(output)} symbols)")


if __name__ == "__main__":
    main()
