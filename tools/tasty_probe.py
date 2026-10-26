"""One-session probe of the Tastytrade DXLink feed for SPX 0DTE options.

Answers one question before anything is built on the feed: does it give a
real-time, complete trade tape (TimeAndSale) with the bid/ask needed to tell
which side traded, plus live Greeks and open interest?

Needs a funded Tastytrade account and two environment variables (never commit
or print them):
    TASTY_CLIENT_SECRET   OAuth client secret (read scope is enough)
    TASTY_REFRESH_TOKEN   personal grant refresh token

Run during US market hours:
    pip install "tastytrade>=13,<14"
    python tools/tasty_probe.py --minutes 10 --strikes 30 --out tasty_probe.json
    python tools/tasty_probe.py --symbol SPY --minutes 8   # multi-exchange, shows sweeps
    python tools/tasty_probe.py --check     # login + chain + stream only, any time
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

SWEEP_WINDOW_MS = 500


def trade_side(price, bid, ask) -> str:
    """Side of a print judged against the quote at the time of the trade."""
    if price is None or bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return "unknown"
    if price >= ask:
        return "buy"
    if price <= bid:
        return "sell"
    mid = (bid + ask) / 2
    if price > mid:
        return "lean_buy"
    if price < mid:
        return "lean_sell"
    return "mid"


def count_sweeps(prints: list[dict]) -> int:
    """Clusters of same-symbol, same-side prints on 2+ exchanges within 500 ms."""
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for p in prints:
        if p["side"] in ("buy", "sell"):
            by_key[(p["symbol"], p["side"])].append(p)
    sweeps = 0
    for group in by_key.values():
        group.sort(key=lambda p: p["time"])
        i = 0
        while i < len(group):
            j = i
            while j + 1 < len(group) and group[j + 1]["time"] - group[i]["time"] <= SWEEP_WINDOW_MS:
                j += 1
            if len({p["exchange"] for p in group[i : j + 1]}) >= 2:
                sweeps += 1
            i = j + 1
    return sweeps


def median_or_none(values):
    return round(statistics.median(values), 3) if values else None


def summarize(prints, tape_lags_s, greek_times, day_volumes, open_interest, subscribed, counts) -> dict:
    sides = Counter(p["side"] for p in prints)
    aggressor = Counter(p["aggressor"] for p in prints)
    tape_volume: dict[str, int] = defaultdict(int)
    for p in prints:
        if p["valid"]:
            tape_volume[p["symbol"]] += p["size"]
    coverage = []
    for sym, (first, last) in day_volumes.items():
        traded = last - first
        if traded > 0:
            coverage.append(min(tape_volume.get(sym, 0) / traded, 2.0))
    greek_gaps = []
    for times in greek_times.values():
        times.sort()
        greek_gaps += [(b - a) / 1000 for a, b in zip(times, times[1:]) if b > a]
    total = len(prints) or 1
    return {
        "subscribed_contracts": subscribed,
        "events": dict(counts),
        "tape_lag_seconds_median": median_or_none(tape_lags_s),
        "realtime": (median_or_none(tape_lags_s) or 999) < 60,
        "tape": {
            "prints": len(prints),
            "contracts_with_prints": len({p["symbol"] for p in prints}),
            "valid_tick_share": round(sum(p["valid"] for p in prints) / total, 3),
            "has_bid_ask_share": round(sum(p["side"] != "unknown" for p in prints) / total, 3),
            "spread_leg_share": round(sum(p["spread_leg"] for p in prints) / total, 3),
            "side_from_quote": dict(sides),
            "feed_aggressor_side": dict(aggressor),
            "exchanges": dict(Counter(p["exchange"] for p in prints)),
            "sweep_like_clusters": count_sweeps(prints),
            "volume_coverage_median": median_or_none(coverage),
        },
        "greeks": {
            "contracts_updated": len(greek_times),
            "seconds_between_updates_median": median_or_none(greek_gaps),
        },
        "open_interest": {
            "contracts_with_oi": sum(1 for v in open_interest.values() if v),
            "total": sum(open_interest.values()),
        },
    }


def pick_contracts(chain, spot: float, strikes: int, underlying: str = "SPX"):
    today = date.today()
    expiries = sorted(d for d in chain if d >= today)
    if not expiries:
        raise SystemExit(f"No {underlying} expiries found in the chain.")
    expiry = expiries[0]
    options = chain[expiry]
    if underlying == "SPX":
        options = [o for o in options if o.streamer_symbol.startswith(".SPXW")] or options
    by_strike = sorted({float(o.strike_price) for o in options}, key=lambda k: abs(k - spot))[: strikes * 2 + 1]
    keep = set(by_strike)
    return expiry, [o.streamer_symbol for o in options if float(o.strike_price) in keep]


async def underlying_spot(streamer, Summary, Trade, underlying: str) -> float | None:
    """Last trade of the underlying, or the latest close when it is not printing."""
    await streamer.subscribe(Trade, [underlying])
    await streamer.subscribe(Summary, [underlying])
    try:
        trade = await asyncio.wait_for(streamer.get_event(Trade), timeout=20)
        if trade.price:
            return float(trade.price)
    except asyncio.TimeoutError:
        pass
    try:
        summary = await asyncio.wait_for(streamer.get_event(Summary), timeout=10)
    except asyncio.TimeoutError:
        return None
    close = summary.day_close_price or summary.prev_day_close_price
    return float(close) if close else None


async def connection_check(streamer, Greeks, Quote, Summary, symbols, spot, expiry) -> dict:
    """Login, chain and stream work; first events for a few contracts. Market need not be open."""
    sample = symbols[: min(10, len(symbols))]
    seen = {}
    for cls in (Quote, Greeks, Summary):
        await streamer.subscribe(cls, sample)
        try:
            event = await asyncio.wait_for(streamer.get_event(cls), timeout=20)
            seen[cls.__name__] = event.event_symbol
        except asyncio.TimeoutError:
            seen[cls.__name__] = None
    return {"check": "ok", "spot": spot, "expiry": str(expiry), "contracts": len(symbols), "first_event": seen}


async def run(minutes: float, strikes: int, check_only: bool = False, underlying: str = "SPX") -> dict:
    from tastytrade import DXLinkStreamer, Session
    from tastytrade.dxfeed import Greeks, Quote, Summary, TimeAndSale, Trade
    from tastytrade.instruments import get_option_chain

    secret = os.environ.get("TASTY_CLIENT_SECRET")
    refresh = os.environ.get("TASTY_REFRESH_TOKEN")
    if not secret or not refresh:
        raise SystemExit("Set TASTY_CLIENT_SECRET and TASTY_REFRESH_TOKEN first.")

    session = Session(secret, refresh)
    chain = await get_option_chain(session, underlying)

    prints: list[dict] = []
    tape_lags: list[float] = []
    greek_times: dict[str, list[int]] = defaultdict(list)
    day_volumes: dict[str, list[int]] = {}
    open_interest: dict[str, int] = {}
    counts: Counter = Counter()
    last_quote: dict[str, tuple] = {}

    async with DXLinkStreamer(session) as streamer:
        spot = await underlying_spot(streamer, Summary, Trade, underlying)
        if not spot:
            raise SystemExit(f"No {underlying} price from the streamer; is the account fully onboarded?")
        expiry, symbols = pick_contracts(chain, spot, strikes, underlying)
        print(f"{underlying} {spot:.2f}, expiry {expiry}, {len(symbols)} contracts", file=sys.stderr)
        if check_only:
            return await connection_check(streamer, Greeks, Quote, Summary, symbols, spot, expiry)

        wanted = set(symbols)
        for cls in (Quote, Greeks, Summary, Trade, TimeAndSale):
            await streamer.subscribe(cls, symbols)

        async def quotes():
            async for q in streamer.listen(Quote):
                counts["Quote"] += 1
                last_quote[q.event_symbol] = (q.bid_price, q.ask_price)

        async def greeks():
            async for g in streamer.listen(Greeks):
                counts["Greeks"] += 1
                greek_times[g.event_symbol].append(g.time or g.event_time or int(time.time() * 1000))

        async def summaries():
            async for s in streamer.listen(Summary):
                if s.event_symbol not in wanted:
                    continue
                counts["Summary"] += 1
                open_interest[s.event_symbol] = s.open_interest or 0

        async def trades():
            async for t in streamer.listen(Trade):
                if t.event_symbol not in wanted:
                    continue
                counts["Trade"] += 1
                if t.day_volume is not None:
                    vol = int(t.day_volume)
                    first = day_volumes.get(t.event_symbol, [vol, vol])[0]
                    day_volumes[t.event_symbol] = [first, vol]

        async def tape():
            async for p in streamer.listen(TimeAndSale):
                counts["TimeAndSale"] += 1
                if p.time:
                    tape_lags.append(time.time() - p.time / 1000)
                bid, ask = p.bid_price, p.ask_price
                if not bid and not ask and p.event_symbol in last_quote:
                    bid, ask = last_quote[p.event_symbol]
                prints.append(
                    {
                        "symbol": p.event_symbol,
                        "time": p.time,
                        "exchange": p.exchange_code,
                        "size": int(p.size or 0),
                        "side": trade_side(p.price, bid, ask),
                        "aggressor": p.aggressor_side or "NONE",
                        "spread_leg": bool(p.spread_leg),
                        "valid": bool(p.valid_tick),
                    }
                )

        tasks = [asyncio.create_task(f()) for f in (quotes, greeks, summaries, trades, tape)]
        await asyncio.sleep(minutes * 60)
        for t in tasks:
            t.cancel()

    report = summarize(prints, tape_lags, greek_times, day_volumes, open_interest, len(symbols), counts)
    report["underlying"] = underlying
    report["spot"] = spot
    report["expiry"] = str(expiry)
    report["minutes"] = minutes
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--strikes", type=int, default=30, help="strikes each side of spot")
    parser.add_argument("--out", help="also write the report to this JSON file")
    parser.add_argument("--symbol", default="SPX", help="underlying, e.g. SPX, SPY, QQQ")
    parser.add_argument("--check", action="store_true", help="only test login, chain and stream (works when closed)")
    args = parser.parse_args()
    report = asyncio.run(run(args.minutes, args.strikes, args.check, args.symbol.upper()))
    text = json.dumps(report, default=lambda v: float(v) if isinstance(v, Decimal) else str(v))
    print("PROBE_REPORT " + text, flush=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    main()
