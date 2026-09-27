"""Backtest SPXW put credit spreads over the chain store.

The regime backtester (tools/backtest_regime.py) asks "was the Engine Label
right?". This one asks "how much money?": it replays the Baseline -- a 7DTE
SPXW put credit spread entered every session at 10:00 ET -- against the
normalized chain store and reports after-cost P&L.

One configuration per run:
- expiry: the SPXW expiry nearest to seven calendar days after the session
  (ties go to the earlier expiry);
- short strike: the put whose delta, from the canonical greeks engine and the
  snapshot IV, is closest to -short_delta; long strike = short - width;
- held to expiry, settled at SPXW PM settlement (the SPX close on the expiry
  date, from the chain store's daily_close series);
- fills cross fill_ratio of the half-spread from mid (0 = mid, 0.5 = the
  default, 1 = the far side of the quote);
- commissions per contract on entry at IBKR tiered rates plus an exchange fee;
  cash settlement at expiry costs nothing;
- contracts sized so the spread's maximum loss (width - credit, plus entry
  costs) is at most risk_pct of equity. Equity is the starting equity plus the
  P&L of every trade settled before the session.

optopsy builds the spreads, applies the fill model and computes the exit
proceeds. The loader feeds it only the two chosen legs per session plus one
synthetic exit row per leg on the expiry date, quoted at intrinsic value
against the settlement, so optopsy's exit is the PM settlement.

Usage:
    python tools/backtest_spread.py --store PATH [--start DATE] [--end DATE]
        [--short-delta 0.16] [--width 25] [--equity 1000000]
        [--risk-pct 0.01] [--fill-ratio 0.5] [--out report.json]

The JSON report is printed to stdout (and written to --out when given).
"""
import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import optopsy
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
from tripity_experiment import chain_store
from tripity_experiment import matrix_gex

ET = matrix_gex.ET

# --- Execution rules (named constants; tune here only) ---
MARKET_OPEN = (9, 30)  # ET; snapshots before this never count as the entry
# An entry snapshot staler than this is skipped rather than traded.
ENTRY_MAX_AGE_MINUTES = 45
CONTRACT_MULTIPLIER = 100
# IBKR Pro tiered option commissions (<= 10,000 contracts/month), per contract,
# keyed by the minimum premium of the tier.
COMMISSION_TIERS = ((0.10, 0.65), (0.05, 0.50), (0.0, 0.25))
# Approximate Cboe SPXW customer transaction fee plus clearing/regulatory
# pass-through, per contract. Check against current fee schedules.
EXCHANGE_FEE_PER_CONTRACT = 0.60
TRADING_DAYS = 252
CVAR_TAIL = 0.05

# optopsy settings: one entry row per leg per session, exit on the expiry date.
# With volume at the reference volume, optopsy's liquidity slippage crosses
# exactly fill_ratio of the half-spread.
OPTOPSY_REFERENCE_VOLUME = 1000
OPTOPSY_MAX_ENTRY_DTE = 60
OPTOPSY_MIN_BID = 1e-9  # optopsy wants a positive float; legs need a bid > 0


@dataclass(frozen=True)
class SpreadConfig:
    short_delta: float = 0.16
    width: float = 25.0
    target_dte: int = 7
    entry_time: tuple = (10, 0)
    fill_ratio: float = 0.5
    initial_equity: float = 1_000_000.0
    risk_pct: float = 0.01
    root: str = "SPXW"
    settlement_symbol: str = "SPX"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _session_of(snapshot_ms):
    return datetime.fromtimestamp(snapshot_ms / 1000, tz=ET).date()


def _et_ms(day, hour, minute):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


def load_store(path, config, start=None, end=None):
    """({session: [ChainRow, ...]}, {date: settlement close}) from a chain store."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"chain store not found: {path}")
    start_ms = _et_ms(date.fromisoformat(start), 0, 0) if start else None
    end_ms = _et_ms(date.fromisoformat(end), 23, 59) if end else None
    connection = chain_store.connect(path)
    try:
        rows = chain_store.read_rows(connection, config.root, start_ms, end_ms)
        closes = chain_store.read_daily_closes(connection, config.settlement_symbol)
    finally:
        connection.close()
    sessions = defaultdict(list)
    for row in rows:
        sessions[_session_of(row.snapshot_ms)].append(row)
    return dict(sessions), {date.fromisoformat(d): c for d, c in closes.items()}


# ---------------------------------------------------------------------------
# Trade selection (one candidate per session)
# ---------------------------------------------------------------------------
def entry_snapshot(rows, session, entry_time):
    """Latest snapshot time at/before the entry time and after the open, or
    None when there is none or it is staler than ENTRY_MAX_AGE_MINUTES."""
    entry_ms = _et_ms(session, *entry_time)
    open_ms = _et_ms(session, *MARKET_OPEN)
    stamps = {r.snapshot_ms for r in rows if open_ms <= r.snapshot_ms <= entry_ms}
    if not stamps:
        return None
    stamp = max(stamps)
    if entry_ms - stamp > ENTRY_MAX_AGE_MINUTES * 60 * 1000:
        return None
    return stamp


def _put_delta(row, config):
    sigma = matrix_gex.norm_iv(float(row.iv or 0))
    if sigma <= 0 or not row.underlying:
        return None
    T = matrix_gex.years_to_expiry(row.expiry, config.root, row.snapshot_ms)
    return matrix_gex.bs_delta(row.underlying, row.strike, T, sigma, is_call=False)


def _quote_ok(row):
    return (row.bid is not None and row.ask is not None
            and row.bid > 0 and row.ask >= row.bid)


def select_candidate(session, rows, closes, config):
    """(candidate, None) or (None, skip_reason) for one session."""
    stamp = entry_snapshot(rows, session, config.entry_time)
    if stamp is None:
        return None, "no snapshot at/before entry time"
    puts = [r for r in rows if r.snapshot_ms == stamp and r.right == "P"]
    expiries = sorted({r.expiry for r in puts
                       if date.fromisoformat(r.expiry) > session})
    if not expiries:
        return None, "no expiry after the session"
    expiry = min(expiries, key=lambda e: abs(
        (date.fromisoformat(e) - session).days - config.target_dte))
    chain = {r.strike: r for r in puts if r.expiry == expiry}
    deltas = {k: d for k, d in ((k, _put_delta(r, config)) for k, r in chain.items())
              if d is not None}
    if not deltas:
        return None, "no put with a usable IV"
    short_strike = min(deltas, key=lambda k: abs(deltas[k] + config.short_delta))
    long_strike = short_strike - config.width
    if long_strike not in chain:
        return None, f"no {long_strike:g} strike for the long leg"
    short, long_ = chain[short_strike], chain[long_strike]
    if not (_quote_ok(short) and _quote_ok(long_)):
        return None, "missing or crossed quote on a leg"
    settlement = closes.get(date.fromisoformat(expiry))
    if settlement is None:
        return None, f"no {config.settlement_symbol} close to settle {expiry}"
    return {
        "session": session, "expiry": expiry, "short": short, "long": long_,
        "short_delta": deltas[short_strike], "settlement": settlement,
    }, None


# ---------------------------------------------------------------------------
# optopsy: spread construction, fills, settlement exit
# ---------------------------------------------------------------------------
def _optopsy_frame(candidates):
    """Entry rows for the chosen legs plus intrinsic-value exit rows."""
    records = []
    for c in candidates:
        for leg in (c["short"], c["long"]):
            records.append({
                "quote_date": c["session"].isoformat(), "strike": leg.strike,
                "bid": leg.bid, "ask": leg.ask, "underlying_price": leg.underlying,
                "expiration": c["expiry"],
            })
            intrinsic = max(leg.strike - c["settlement"], 0.0)
            records.append({
                "quote_date": c["expiry"], "strike": leg.strike,
                "bid": intrinsic, "ask": intrinsic,
                "underlying_price": c["settlement"], "expiration": c["expiry"],
            })
    frame = pd.DataFrame(records).drop_duplicates(
        subset=["quote_date", "expiration", "strike"])
    return frame.assign(
        underlying_symbol="SPX", option_type="p",
        volume=float(OPTOPSY_REFERENCE_VOLUME),
        quote_date=pd.to_datetime(frame["quote_date"]).astype("datetime64[ns]"),
        expiration=pd.to_datetime(frame["expiration"]).astype("datetime64[ns]"),
        strike=frame["strike"].astype(float),
        bid=frame["bid"].astype(float), ask=frame["ask"].astype(float),
        underlying_price=frame["underlying_price"].astype(float),
    )


def price_candidates(candidates, fill_ratio):
    """Per candidate: leg fills, credit and settlement value per share."""
    if not candidates:
        return []
    frame = _optopsy_frame(candidates)
    params = {
        "raw": True, "exit_dte": 0, "max_entry_dte": OPTOPSY_MAX_ENTRY_DTE,
        "dte_interval": 1, "min_bid_ask": OPTOPSY_MIN_BID,
        "slippage": "liquidity", "fill_ratio": float(fill_ratio),
        "reference_volume": OPTOPSY_REFERENCE_VOLUME,
    }
    spreads = optopsy.short_put_spread(frame, **params)
    shorts = optopsy.short_puts(frame, **params)
    longs = optopsy.long_puts(frame, **params)

    def key(expiration, dte, strike):
        return (pd.Timestamp(expiration).date().isoformat(), int(dte), float(strike))

    short_fill = {key(r.expiration, r.dte_entry, r.strike): r.entry
                  for r in shorts.itertuples()}
    long_fill = {key(r.expiration, r.dte_entry, r.strike): r.entry
                 for r in longs.itertuples()}
    spread = {key(r.expiration, r.dte_entry, r.strike_leg2): r
              for r in spreads.itertuples()
              if r.strike_leg2 - r.strike_leg1 > 0}
    priced = []
    for c in candidates:
        dte = (date.fromisoformat(c["expiry"]) - c["session"]).days
        k_short = key(c["expiry"], dte, c["short"].strike)
        k_long = key(c["expiry"], dte, c["long"].strike)
        row = spread.get(k_short)
        if row is None or row.strike_leg1 != c["long"].strike:
            continue
        priced.append({
            **c,
            "short_fill": float(short_fill[k_short]),
            "long_fill": float(long_fill[k_long]),
            "credit": float(-row.total_entry_cost),
            "settlement_value": float(-row.total_exit_proceeds),
        })
    return priced


# ---------------------------------------------------------------------------
# Costs and sizing
# ---------------------------------------------------------------------------
def commission_per_contract(premium):
    for floor, rate in COMMISSION_TIERS:
        if premium >= floor:
            return rate
    return COMMISSION_TIERS[-1][1]


def entry_costs_per_spread(short_fill, long_fill):
    return sum(commission_per_contract(p) + EXCHANGE_FEE_PER_CONTRACT
               for p in (short_fill, long_fill))


def size_trades(priced, config):
    """Walk sessions in order, sizing each trade on equity settled so far."""
    trades, skipped = [], []
    for c in sorted(priced, key=lambda c: c["session"]):
        equity = config.initial_equity + sum(
            t["pnl"] for t in trades
            if date.fromisoformat(t["expiry"]) < c["session"])
        costs = entry_costs_per_spread(c["short_fill"], c["long_fill"])
        width = c["short"].strike - c["long"].strike
        max_loss = (width - c["credit"]) * CONTRACT_MULTIPLIER + costs
        contracts = math.floor(config.risk_pct * equity / max_loss) if max_loss > 0 else 0
        if contracts < 1:
            skipped.append([c["session"].isoformat(),
                            "max loss of one spread exceeds risk budget"])
            continue
        pnl_per_spread = ((c["credit"] - c["settlement_value"]) * CONTRACT_MULTIPLIER
                          - costs)
        trades.append({
            "session": c["session"].isoformat(),
            "expiry": c["expiry"],
            "short_strike": c["short"].strike,
            "long_strike": c["long"].strike,
            "short_delta": round(c["short_delta"], 6),
            "underlying_at_entry": c["short"].underlying,
            "short_fill": round(c["short_fill"], 6),
            "long_fill": round(c["long_fill"], 6),
            "credit": round(c["credit"], 6),
            "costs_per_spread": round(costs, 6),
            "max_loss_per_spread": round(max_loss, 6),
            "equity_at_entry": round(equity, 2),
            "contracts": contracts,
            "settlement": c["settlement"],
            "pnl": round(contracts * pnl_per_spread, 2),
        })
    return trades, skipped


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_metrics(trades, sessions, initial_equity):
    """Risk/return summary. Returns are daily, realized at settlement."""
    pnls = [t["pnl"] for t in trades]
    metrics = {
        "trades": len(trades),
        "sharpe": None, "sortino": None, "max_drawdown": None,
        "worst_week": None, "cvar_5": None, "win_rate": None,
        "avg_pnl_per_trade": None,
    }
    if not trades:
        return metrics
    settled = defaultdict(float)
    for t in trades:
        settled[t["expiry"]] += t["pnl"]
    days = sorted({d.isoformat() for d in sessions} | set(settled))
    equity = peak = initial_equity
    returns, drawdown = [], 0.0
    for day in days:
        returns.append(settled.get(day, 0.0) / equity)
        equity += settled.get(day, 0.0)
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    mean = statistics.fmean(returns)
    if len(returns) > 1 and statistics.stdev(returns) > 0:
        metrics["sharpe"] = mean / statistics.stdev(returns) * math.sqrt(TRADING_DAYS)
    downside = math.sqrt(statistics.fmean(min(r, 0.0) ** 2 for r in returns))
    if downside > 0:
        metrics["sortino"] = mean / downside * math.sqrt(TRADING_DAYS)
    weeks = defaultdict(float)
    for day, pnl in settled.items():
        weeks[date.fromisoformat(day).isocalendar()[:2]] += pnl
    tail = sorted(pnls)[:max(1, math.ceil(CVAR_TAIL * len(pnls)))]
    metrics.update({
        "max_drawdown": drawdown,
        "worst_week": min(weeks.values()),
        "cvar_5": statistics.fmean(tail),
        "win_rate": sum(p > 0 for p in pnls) / len(pnls),
        "avg_pnl_per_trade": statistics.fmean(pnls),
    })
    return metrics


# ---------------------------------------------------------------------------
# The experiment run
# ---------------------------------------------------------------------------
def run_experiment(store_path, config, start=None, end=None):
    """Replay one configuration over the chain store; return the report dict."""
    sessions, closes = load_store(store_path, config, start, end)
    candidates, skipped = [], []
    for session in sorted(sessions):
        candidate, reason = select_candidate(session, sessions[session], closes, config)
        if candidate is None:
            skipped.append([session.isoformat(), reason])
        else:
            candidates.append(candidate)
    priced = price_candidates(candidates, config.fill_ratio)
    priced_sessions = {c["session"] for c in priced}
    skipped += [[c["session"].isoformat(), "optopsy built no spread"]
                for c in candidates if c["session"] not in priced_sessions]
    trades, sizing_skips = size_trades(priced, config)
    skipped = sorted(skipped + sizing_skips)
    return {
        "config": asdict(config),
        "sessions": len(sessions),
        "metrics": compute_metrics(trades, sessions, config.initial_equity),
        "trades": trades,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Backtest a 7DTE SPXW put credit spread over the chain store.")
    parser.add_argument("--store", required=True, help="Path to the chain store SQLite file")
    parser.add_argument("--start", help="First session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--end", help="Last session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--short-delta", type=float, default=SpreadConfig.short_delta)
    parser.add_argument("--width", type=float, default=SpreadConfig.width)
    parser.add_argument("--equity", type=float, default=SpreadConfig.initial_equity)
    parser.add_argument("--risk-pct", type=float, default=SpreadConfig.risk_pct)
    parser.add_argument("--fill-ratio", type=float, default=SpreadConfig.fill_ratio,
                        help="Share of the half-spread crossed from mid (0..1)")
    parser.add_argument("--out", help="Also write the JSON report to this path")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = SpreadConfig(short_delta=args.short_delta, width=args.width,
                          initial_equity=args.equity, risk_pct=args.risk_pct,
                          fill_ratio=args.fill_ratio)
    try:
        for bound in (args.start, args.end):
            if bound:
                date.fromisoformat(bound)
        report = run_experiment(args.store, config, args.start, args.end)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"backtest_spread: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0 if report["trades"] else 1


if __name__ == "__main__":
    sys.exit(main())
