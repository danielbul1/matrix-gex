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
- commissions on entry at IBKR tiered rates (with the per-leg order minimum)
  plus Cboe SPXW fees by premium tier; cash settlement at expiry costs nothing;
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
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from pathlib import Path

import optopsy
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
from tripity_experiment import chain_store
from tripity_experiment import matrix_gex

ET = matrix_gex.ET

# --- The Baseline (fixed for every run) ---
ROOT = "SPXW"
SETTLEMENT_SYMBOL = "SPX"  # the SPX close on the expiry date settles SPXW
TARGET_DTE = 7  # calendar days
ENTRY_TIME = (10, 0)  # ET

# --- Execution rules (named constants; tune here only) ---
MARKET_OPEN = (9, 30)  # ET; snapshots before this never count as the entry
# An entry snapshot staler than this is skipped rather than traded.
ENTRY_MAX_AGE_MINUTES = 45
CONTRACT_MULTIPLIER = 100
# IBKR Pro tiered option commissions (<= 10,000 contracts/month), per contract,
# keyed by the minimum premium of the tier.
COMMISSION_TIERS = ((0.10, 0.65), (0.05, 0.50), (0.0, 0.25))
# IBKR's order minimum, charged on each leg separately, combo orders included.
MIN_COMMISSION_PER_LEG = 1.00
# Cboe SPXW public-customer fees IBKR passes through, per contract, keyed by
# the minimum premium: exchange fee ($0.45 / $0.36) + SPXW execution
# surcharge ($0.14) + ORF ($0.01248) + trade processing ($0.0025).
# The SPX index surcharge is $0 for customers; OCC clearing is not included.
EXCHANGE_FEE_TIERS = ((1.00, 0.60498), (0.0, 0.51498))
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
    fill_ratio: float = 0.5
    initial_equity: float = 1_000_000.0
    risk_pct: float = 0.01


@dataclass(frozen=True)
class Candidate:
    """The spread one session would trade, before fills."""
    session: date
    expiry: date
    short_leg: chain_store.ChainRow
    long_leg: chain_store.ChainRow
    short_delta: float
    settlement: float


@dataclass(frozen=True)
class PricedSpread(Candidate):
    """A candidate with optopsy's fills and settlement value, per share."""
    short_fill: float
    long_fill: float
    credit: float
    settlement_value: float


def _skip(session, reason):
    return {"session": session.isoformat(), "reason": reason}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _session_of(snapshot_ms):
    return datetime.fromtimestamp(snapshot_ms / 1000, tz=ET).date()


def _et_ms(day, hour, minute):
    return int(datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=ET).timestamp() * 1000)


def load_store(path, start=None, end=None):
    """({session: [ChainRow, ...]}, {date: settlement close}) from a chain store.
    start/end are inclusive session dates."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"chain store not found: {path}")
    start_ms = _et_ms(start, 0, 0) if start else None
    end_ms = _et_ms(end, 23, 59) if end else None
    connection = chain_store.connect(path)
    try:
        rows = chain_store.read_rows(connection, ROOT, start_ms, end_ms)
        closes = chain_store.read_daily_closes(connection, SETTLEMENT_SYMBOL)
    finally:
        connection.close()
    sessions = defaultdict(list)
    for row in rows:
        sessions[_session_of(row.snapshot_ms)].append(row)
    return dict(sessions), {date.fromisoformat(d): c for d, c in closes.items()}


# ---------------------------------------------------------------------------
# Trade selection (one candidate per session)
# ---------------------------------------------------------------------------
def entry_snapshot(rows, session):
    """Latest snapshot time at/before ENTRY_TIME and after the open, or None
    when there is none or it is staler than ENTRY_MAX_AGE_MINUTES."""
    entry_ms = _et_ms(session, *ENTRY_TIME)
    open_ms = _et_ms(session, *MARKET_OPEN)
    stamps = {r.snapshot_ms for r in rows if open_ms <= r.snapshot_ms <= entry_ms}
    if not stamps:
        return None
    stamp = max(stamps)
    if entry_ms - stamp > ENTRY_MAX_AGE_MINUTES * 60 * 1000:
        return None
    return stamp


def _put_delta(row):
    sigma = matrix_gex.norm_iv(float(row.iv or 0))
    if sigma <= 0 or not row.underlying:
        return None
    T = matrix_gex.years_to_expiry(row.expiry, ROOT, row.snapshot_ms)
    return matrix_gex.bs_delta(row.underlying, row.strike, T, sigma, is_call=False)


def _quote_ok(row):
    return (row.bid is not None and row.ask is not None
            and row.bid > 0 and row.ask >= row.bid)


def select_candidate(session, rows, closes, config):
    """(Candidate, None) or (None, skip_reason) for one session."""
    stamp = entry_snapshot(rows, session)
    if stamp is None:
        return None, "no snapshot at/before entry time"
    puts = [r for r in rows if r.snapshot_ms == stamp and r.right == "P"]
    expiries = sorted({e for e in (date.fromisoformat(r.expiry) for r in puts)
                       if e > session})
    if not expiries:
        return None, "no expiry after the session"
    expiry = min(expiries, key=lambda e: abs((e - session).days - TARGET_DTE))
    chain = {r.strike: r for r in puts if r.expiry == expiry.isoformat()}
    deltas = {strike: delta for strike, delta
              in ((strike, _put_delta(row)) for strike, row in chain.items())
              if delta is not None}
    if not deltas:
        return None, "no put with a usable IV"
    short_strike = min(deltas, key=lambda strike: abs(deltas[strike] + config.short_delta))
    long_strike = short_strike - config.width
    if long_strike not in chain:
        return None, f"no {long_strike:g} strike for the long leg"
    short_leg, long_leg = chain[short_strike], chain[long_strike]
    if not (_quote_ok(short_leg) and _quote_ok(long_leg)):
        return None, "missing or crossed quote on a leg"
    settlement = closes.get(expiry)
    if settlement is None:
        return None, f"no {SETTLEMENT_SYMBOL} close to settle {expiry.isoformat()}"
    return Candidate(session=session, expiry=expiry, short_leg=short_leg,
                     long_leg=long_leg, short_delta=deltas[short_strike],
                     settlement=settlement), None


# ---------------------------------------------------------------------------
# optopsy: spread construction, fills, settlement exit
# ---------------------------------------------------------------------------
def _optopsy_frame(candidates):
    """Entry rows for the chosen legs plus intrinsic-value exit rows."""
    records = []
    for candidate in candidates:
        expiry = candidate.expiry.isoformat()
        for leg in (candidate.short_leg, candidate.long_leg):
            records.append({
                "quote_date": candidate.session.isoformat(), "strike": leg.strike,
                "bid": leg.bid, "ask": leg.ask, "underlying_price": leg.underlying,
                "expiration": expiry,
            })
            intrinsic = max(leg.strike - candidate.settlement, 0.0)
            records.append({
                "quote_date": expiry, "strike": leg.strike,
                "bid": intrinsic, "ask": intrinsic,
                "underlying_price": candidate.settlement, "expiration": expiry,
            })
    frame = pd.DataFrame(records).drop_duplicates(
        subset=["quote_date", "expiration", "strike"])
    return frame.assign(
        underlying_symbol=SETTLEMENT_SYMBOL, option_type="p",
        volume=float(OPTOPSY_REFERENCE_VOLUME),
        quote_date=pd.to_datetime(frame["quote_date"]).astype("datetime64[ns]"),
        expiration=pd.to_datetime(frame["expiration"]).astype("datetime64[ns]"),
        strike=frame["strike"].astype(float),
        bid=frame["bid"].astype(float), ask=frame["ask"].astype(float),
        underlying_price=frame["underlying_price"].astype(float),
    )


def _leg_key(expiration, dte, strike):
    """Joins optopsy's output rows back to a candidate's legs."""
    return (pd.Timestamp(expiration).date(), int(dte), float(strike))


def _entry_prices(single_legs):
    """{leg key: fill} from optopsy's single-leg output."""
    return {_leg_key(r.expiration, r.dte_entry, r.strike): r.entry
            for r in single_legs.itertuples()}


def price_candidates(candidates, fill_ratio):
    """A PricedSpread for every candidate optopsy could build."""
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
    short_fills = _entry_prices(optopsy.short_puts(frame, **params))
    long_fills = _entry_prices(optopsy.long_puts(frame, **params))
    spread_by_short_leg = {_leg_key(r.expiration, r.dte_entry, r.strike_leg2): r
                           for r in spreads.itertuples()
                           if r.strike_leg2 - r.strike_leg1 > 0}
    priced = []
    for candidate in candidates:
        dte = (candidate.expiry - candidate.session).days
        short_key = _leg_key(candidate.expiry, dte, candidate.short_leg.strike)
        long_key = _leg_key(candidate.expiry, dte, candidate.long_leg.strike)
        spread = spread_by_short_leg.get(short_key)
        if spread is None or spread.strike_leg1 != candidate.long_leg.strike:
            continue
        priced.append(PricedSpread(
            **{f.name: getattr(candidate, f.name) for f in fields(Candidate)},
            short_fill=float(short_fills[short_key]),
            long_fill=float(long_fills[long_key]),
            credit=float(-spread.total_entry_cost),
            settlement_value=float(-spread.total_exit_proceeds),
        ))
    return priced


# ---------------------------------------------------------------------------
# Costs and sizing
# ---------------------------------------------------------------------------
def _tier_rate(tiers, premium):
    for floor, rate in tiers:
        if premium >= floor:
            return rate
    return tiers[-1][1]


def entry_costs(short_fill, long_fill, contracts):
    """Commissions and exchange fees for opening `contracts` spreads."""
    return sum(max(contracts * _tier_rate(COMMISSION_TIERS, p), MIN_COMMISSION_PER_LEG)
               + contracts * _tier_rate(EXCHANGE_FEE_TIERS, p)
               for p in (short_fill, long_fill))


def contracts_within_budget(budget, risk_per_spread, short_fill, long_fill):
    """Most spreads whose total max loss, entry costs included, fits budget."""
    # Costs without the order minimum give an upper bound; step down while the
    # minimum pushes the total over budget (it only binds on small orders).
    floor_cost = sum(_tier_rate(COMMISSION_TIERS, p) + _tier_rate(EXCHANGE_FEE_TIERS, p)
                     for p in (short_fill, long_fill))
    if risk_per_spread + floor_cost <= 0:
        return 0
    contracts = math.floor(budget / (risk_per_spread + floor_cost))
    while contracts >= 1 and (contracts * risk_per_spread
                              + entry_costs(short_fill, long_fill, contracts)) > budget:
        contracts -= 1
    return contracts


def size_trades(priced, config):
    """Walk sessions in order, sizing each trade on equity settled so far."""
    trades, skipped = [], []
    realized = []  # (expiry, pnl) of every trade taken so far
    for spread in sorted(priced, key=lambda spread: spread.session):
        equity = config.initial_equity + sum(
            pnl for expiry, pnl in realized if expiry < spread.session)
        width = spread.short_leg.strike - spread.long_leg.strike
        risk_per_spread = (width - spread.credit) * CONTRACT_MULTIPLIER
        contracts = contracts_within_budget(config.risk_pct * equity, risk_per_spread,
                                            spread.short_fill, spread.long_fill)
        if contracts < 1:
            skipped.append(_skip(spread.session,
                                 "max loss of one spread exceeds risk budget"))
            continue
        costs = entry_costs(spread.short_fill, spread.long_fill, contracts) / contracts
        pnl_per_spread = ((spread.credit - spread.settlement_value) * CONTRACT_MULTIPLIER
                          - costs)
        pnl = round(contracts * pnl_per_spread, 2)
        realized.append((spread.expiry, pnl))
        trades.append({
            "session": spread.session.isoformat(),
            "expiry": spread.expiry.isoformat(),
            "short_strike": spread.short_leg.strike,
            "long_strike": spread.long_leg.strike,
            "short_delta": round(spread.short_delta, 6),
            "underlying_at_entry": spread.short_leg.underlying,
            "short_fill": round(spread.short_fill, 6),
            "long_fill": round(spread.long_fill, 6),
            "credit": round(spread.credit, 6),
            "costs_per_spread": round(costs, 6),
            "max_loss_per_spread": round(risk_per_spread + costs, 6),
            "equity_at_entry": round(equity, 2),
            "contracts": contracts,
            "settlement": spread.settlement,
            "pnl": pnl,
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
    pnl_by_expiry = defaultdict(float)
    for t in trades:
        pnl_by_expiry[date.fromisoformat(t["expiry"])] += t["pnl"]
    days = sorted(set(sessions) | set(pnl_by_expiry))
    equity = peak = initial_equity
    returns, drawdown = [], 0.0
    for day in days:
        returns.append(pnl_by_expiry.get(day, 0.0) / equity)
        equity += pnl_by_expiry.get(day, 0.0)
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    mean = statistics.fmean(returns)
    if len(returns) > 1 and statistics.stdev(returns) > 0:
        metrics["sharpe"] = mean / statistics.stdev(returns) * math.sqrt(TRADING_DAYS)
    downside = math.sqrt(statistics.fmean(min(r, 0.0) ** 2 for r in returns))
    if downside > 0:
        metrics["sortino"] = mean / downside * math.sqrt(TRADING_DAYS)
    weeks = defaultdict(float)
    for day, pnl in pnl_by_expiry.items():
        weeks[day.isocalendar()[:2]] += pnl
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
    """Replay one configuration over the chain store; return the report dict.
    start/end are inclusive session dates."""
    sessions, closes = load_store(store_path, start, end)
    candidates, skipped = [], []
    for session in sorted(sessions):
        candidate, reason = select_candidate(session, sessions[session], closes, config)
        if candidate is None:
            skipped.append(_skip(session, reason))
        else:
            candidates.append(candidate)
    priced = price_candidates(candidates, config.fill_ratio)
    priced_sessions = {spread.session for spread in priced}
    skipped += [_skip(candidate.session, "optopsy built no spread")
                for candidate in candidates if candidate.session not in priced_sessions]
    trades, sizing_skips = size_trades(priced, config)
    return {
        "config": asdict(config),
        "sessions": len(sessions),
        "metrics": compute_metrics(trades, sessions, config.initial_equity),
        "trades": trades,
        "skipped": sorted(skipped + sizing_skips, key=lambda skip: skip["session"]),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Backtest a 7DTE SPXW put credit spread over the chain store.")
    parser.add_argument("--store", required=True, help="Path to the chain store SQLite file")
    parser.add_argument("--start", type=date.fromisoformat,
                        help="First session date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="Last session date (YYYY-MM-DD, inclusive)")
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
