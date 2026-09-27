"""Backtest SPXW put credit spreads over the chain store.

The regime backtester (tools/backtest_regime.py) asks "was the Engine Label
right?". This one asks "how much money?": it replays the Baseline -- a 7DTE
SPXW put credit spread entered every session at 10:00 ET -- against the
normalized chain store and reports after-cost P&L.

One configuration per run (or, with --grid, every configuration of the
parameter grid):
- expiry: the SPXW expiry nearest to seven calendar days after the session
  (ties go to the earlier expiry);
- short strike: the put whose delta, from the canonical greeks engine and the
  snapshot IV, is closest to -short_delta; long strike = short - width;
- exit rule "hold": held to expiry, settled at SPXW PM settlement (the SPX
  close on the expiry date, from the chain store's daily_close series);
- exit rule "managed": a 50% take-profit and a 2x credit stop, checked at
  every intraday snapshot after entry (the chain store's 30-minute grid). The
  first snapshot where buying the spread back costs <= 50% of the credit
  takes profit; the first where it costs >= 2x the credit stops out. A spread
  that reaches neither is held to expiry;
- fills cross fill_ratio of the half-spread from mid (0 = mid, 0.5 = the
  default, 1 = the far side of the quote), on entry and on managed exits
  alike, so a stop never fills better than an entry;
- commissions on every order at IBKR tiered rates (with the per-leg order
  minimum) plus Cboe SPXW fees by premium tier; cash settlement at expiry
  costs nothing;
- a new position every session, with at most five open at once;
- contracts sized so the spread's maximum loss (width - credit, plus entry
  costs) is at most risk_pct of equity. Equity is the starting equity plus the
  P&L of every trade closed before the session.

The grid is short delta {0.10, 0.16, 0.20} x width {25, 50} x exit rule
{hold, managed}. Its headline has one row per cell at the 50% fill; the fill
sensitivity table repeats every cell at mid, 50% and full spread.

--gex-filter replays one configuration under each GEX Filter: positive Gamma
Regime, and GEX Percentile above a threshold. Naive GEX is read at the entry
snapshot only (SOD Open Interest, the snapshot's spot and IV, the canonical
greeks engine, calls + / puts -); the percentile ranks it against past
sessions only. Each filter reports its kept sessions, its Rejected Sessions
and the Baseline side by side, all over the sessions it could decide. tools/gex_crosscheck.py checks the Naive GEX
series against SqueezeMetrics.

Each filter is then compared with its IV-Matched Control: the Baseline on the
lowest-VIX sessions (VIX close before the session), as many as the filter
keeps. The comparison is a regression of trade P&L on the filter flag with
VIX, ATM IV and VRP as covariates, a paired block bootstrap of the Sharpe and
mean-P&L differences, and a per-year split. The report opens with the
verdict: the filter beats its control significantly (every interval above
zero) and consistently (every year), or GEX adds nothing beyond VIX.

optopsy builds the spreads, applies the fill model and computes the exit
proceeds. The loader feeds it only the two chosen legs per session plus one
synthetic exit row per leg on the expiry date, quoted at intrinsic value
against the settlement, so optopsy's exit is the PM settlement. optopsy works
on whole days, so managed exits on the intraday grid are priced here, with a
copy of the fill formula optopsy applies to entries (optopsy exposes it only
as a private helper).

Usage:
    python tools/backtest_spread.py --store PATH [--start DATE] [--end DATE]
        [--short-delta 0.16] [--width 25] [--equity 1000000]
        [--risk-pct 0.01] [--fill-ratio 0.5] [--exit-rule hold|managed]
        [--out report.json]
    python tools/backtest_spread.py --store PATH --grid [--start DATE]
        [--end DATE] [--equity 1000000] [--risk-pct 0.01] [--out grid.json]

    python tools/backtest_spread.py --store PATH --gex-filter [--start DATE]
        [--end DATE] [--gex-percentile 0.5] [single-configuration flags]
        [--out gex.json] [--summary gex.md]

The JSON report is printed to stdout (and written to --out when given).
"""
import argparse
import bisect
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta
from itertools import product
from pathlib import Path

import numpy as np
import optopsy
import pandas as pd
from scipy import stats

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
MARKET_CLOSE = (16, 0)  # ET; SPXW PM settlement, and the last managed-exit check
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

# --- Position management ---
RULE_HOLD = "hold"
RULE_MANAGED = "managed"  # take profit / stop below, else hold to expiry
EXIT_RULES = (RULE_HOLD, RULE_MANAGED)
TAKE_PROFIT_SHARE = 0.5  # buy back at <= this share of the credit
STOP_MULTIPLE = 2.0  # buy back at >= this multiple of the credit
# Why a position closed (the trade's "exit" field).
CLOSED_TAKE_PROFIT = "take_profit"
CLOSED_STOP = "stop"
CLOSED_EXPIRY = "expiry"
MAX_OPEN_POSITIONS = 5

# --- The parameter grid ---
GRID_SHORT_DELTAS = (0.10, 0.16, 0.20)
GRID_WIDTHS = (25.0, 50.0)
FILL_LEVELS = (("mid", 0.0), ("50%", 0.5), ("full", 1.0))
HEADLINE_FILL = "50%"

# optopsy settings: one entry row per leg per session, exit on the expiry date.
# With volume at the reference volume, optopsy's liquidity slippage crosses
# exactly fill_ratio of the half-spread.
OPTOPSY_REFERENCE_VOLUME = 1000
OPTOPSY_MAX_ENTRY_DTE = 60
OPTOPSY_MIN_BID = 1e-9  # optopsy wants a positive float; legs need a bid > 0

# --- The GEX Filter ---
GAMMA_POSITIVE = "positive"
GAMMA_NEGATIVE = "negative"
FILTER_POSITIVE_GAMMA = "positive_gamma"
FILTER_GEX_PERCENTILE = "gex_percentile"  # keeps sessions above the threshold
GEX_PERCENTILE_THRESHOLD = 0.5
GEX_PERCENTILE_LOOKBACK = 252  # past sessions
GEX_PERCENTILE_MIN_HISTORY = 20  # past sessions before a percentile exists
# How far before the first session Naive GEX history is read, in calendar
# days: enough to hold GEX_PERCENTILE_LOOKBACK sessions.
GEX_HISTORY_CALENDAR_DAYS = 380
# The report's rows for every filter.
ROW_KEPT = "kept"
ROW_REJECTED = "rejected"
ROW_BASELINE = "baseline"

# --- The IV-Matched Control and the verdict ---
VIX_SYMBOL = "VIX"
ROW_IV_MATCHED = "iv_matched_control"
REALIZED_VOL_WINDOW = 20  # past SPX daily returns behind VRP
REGRESSION_COVARIATES = ("vix", "atm_iv", "vrp")
CONFIDENCE = 0.95  # regression and bootstrap intervals
BOOTSTRAP_RESAMPLES = 2000
# Days per resampled block: a 7DTE trade stays open about five sessions, so
# neighbouring days' returns are not independent.
BOOTSTRAP_BLOCK = 5
BOOTSTRAP_SEED = 7  # the same store always gives the same intervals
VERDICT_POSITIVE = "positive"
VERDICT_NULL = "null"
VERDICT_POSITIVE_LINE = ("VERDICT: the GEX Filter beats its IV-Matched Control"
                         " significantly and consistently across years ({})."
                         )
VERDICT_NULL_LINE = "VERDICT: GEX adds nothing beyond VIX as a trade filter."


@dataclass(frozen=True)
class SpreadConfig:
    short_delta: float = 0.16
    width: float = 25.0
    fill_ratio: float = 0.5
    exit_rule: str = RULE_HOLD
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


@dataclass(frozen=True)
class Exit:
    """How a spread is closed, per share."""
    reason: str  # CLOSED_TAKE_PROFIT, CLOSED_STOP or CLOSED_EXPIRY
    closed_ms: int
    debit: float  # paid to close: the buy-back, or the settlement value
    short_fill: float | None  # the buy-back fills; None when cash-settled
    long_fill: float | None


@dataclass(frozen=True)
class Position:
    """A priced spread and how it will close."""
    spread: PricedSpread
    exit: Exit


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


def load_closes(path, symbol):
    """{date: close} for one daily_close symbol."""
    connection = chain_store.connect(path)
    try:
        closes = chain_store.read_daily_closes(connection, symbol)
    finally:
        connection.close()
    return {date.fromisoformat(d): c for d, c in closes.items()}


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
    """(a PricedSpread for every candidate optopsy could build, a skip for
    every candidate it could not)."""
    if not candidates:
        return [], []
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
            settlement_value=float(-spread.total_exit_proceeds) + 0.0,  # no -0.0
        ))
    priced_sessions = {spread.session for spread in priced}
    return priced, [_skip(candidate.session, "optopsy built no spread")
                    for candidate in candidates
                    if candidate.session not in priced_sessions]


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------
def leg_marks(sessions):
    """{(expiry, strike): {snapshot_ms: ChainRow}} over every put in the store."""
    marks = defaultdict(dict)
    for rows in sessions.values():
        for row in rows:
            if row.right == "P":
                marks[(row.expiry, row.strike)][row.snapshot_ms] = row
    return marks


def _fill(row, buying, fill_ratio):
    """optopsy's liquidity fill at the reference volume: mid, moved fill_ratio
    of the half-spread against us."""
    mid, half_spread = (row.bid + row.ask) / 2, (row.ask - row.bid) / 2
    return mid + half_spread * fill_ratio if buying else mid - half_spread * fill_ratio


def _exit_quote_ok(row):
    # A zero bid is a real quote on a far wing; it can still be sold at the fill.
    return (row.bid is not None and row.ask is not None
            and 0 <= row.bid <= row.ask and row.ask > 0)


def _during_session(snapshot_ms):
    day = _session_of(snapshot_ms)
    return _et_ms(day, *MARKET_OPEN) <= snapshot_ms <= _et_ms(day, *MARKET_CLOSE)


def find_exit(spread, marks, config):
    """The Exit the configuration's exit rule takes for a PricedSpread."""
    settle = Exit(reason=CLOSED_EXPIRY, closed_ms=_et_ms(spread.expiry, *MARKET_CLOSE),
                  debit=spread.settlement_value, short_fill=None, long_fill=None)
    if config.exit_rule == RULE_HOLD:
        return settle
    expiry = spread.expiry.isoformat()
    shorts = marks.get((expiry, spread.short_leg.strike), {})
    longs = marks.get((expiry, spread.long_leg.strike), {})
    for stamp in sorted(shorts.keys() & longs.keys()):
        if not (spread.short_leg.snapshot_ms < stamp <= settle.closed_ms
                and _during_session(stamp)):
            continue
        short, long_ = shorts[stamp], longs[stamp]
        if not (_exit_quote_ok(short) and _exit_quote_ok(long_)):
            continue
        short_fill = _fill(short, buying=True, fill_ratio=config.fill_ratio)
        long_fill = _fill(long_, buying=False, fill_ratio=config.fill_ratio)
        debit = short_fill - long_fill
        if debit <= TAKE_PROFIT_SHARE * spread.credit:
            reason = CLOSED_TAKE_PROFIT
        elif debit >= STOP_MULTIPLE * spread.credit:
            reason = CLOSED_STOP
        else:
            continue
        return Exit(reason=reason, closed_ms=stamp, debit=debit,
                    short_fill=short_fill, long_fill=long_fill)
    return settle


# ---------------------------------------------------------------------------
# Costs and sizing
# ---------------------------------------------------------------------------
def _tier_rate(tiers, premium):
    for floor, rate in tiers:
        if premium >= floor:
            return rate
    return tiers[-1][1]


def order_costs(short_fill, long_fill, contracts):
    """Commissions and exchange fees for opening or closing `contracts` spreads."""
    return sum(max(contracts * _tier_rate(COMMISSION_TIERS, p), MIN_COMMISSION_PER_LEG)
               + contracts * _tier_rate(EXCHANGE_FEE_TIERS, p)
               for p in (short_fill, long_fill))


def _costs_per_spread(short_fill, long_fill, contracts):
    return order_costs(short_fill, long_fill, contracts) / contracts


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
                              + order_costs(short_fill, long_fill, contracts)) > budget:
        contracts -= 1
    return contracts


def _et_iso(snapshot_ms):
    return datetime.fromtimestamp(snapshot_ms / 1000, tz=ET).isoformat()


def size_trades(positions, config):
    """Walk Positions in session order, skipping entries while
    MAX_OPEN_POSITIONS are open and sizing each trade on equity closed so far."""
    trades, skipped = [], []
    taken = []  # (closed_ms, pnl) of every trade taken so far
    for position in sorted(positions, key=lambda position: position.spread.session):
        spread, closing = position.spread, position.exit
        entry_ms = spread.short_leg.snapshot_ms
        if sum(closed_ms > entry_ms for closed_ms, _ in taken) >= MAX_OPEN_POSITIONS:
            skipped.append(_skip(spread.session,
                                 "position cap reached"))
            continue
        equity = config.initial_equity + sum(
            pnl for closed_ms, pnl in taken if _session_of(closed_ms) < spread.session)
        width = spread.short_leg.strike - spread.long_leg.strike
        risk_per_spread = (width - spread.credit) * CONTRACT_MULTIPLIER
        contracts = contracts_within_budget(config.risk_pct * equity, risk_per_spread,
                                            spread.short_fill, spread.long_fill)
        if contracts < 1:
            skipped.append(_skip(spread.session,
                                 "max loss of one spread exceeds risk budget"))
            continue
        entry_costs = _costs_per_spread(spread.short_fill, spread.long_fill, contracts)
        exit_costs = 0.0
        if closing.short_fill is not None:
            exit_costs = _costs_per_spread(closing.short_fill, closing.long_fill,
                                           contracts)
        costs = entry_costs + exit_costs
        pnl_per_spread = (spread.credit - closing.debit) * CONTRACT_MULTIPLIER - costs
        pnl = round(contracts * pnl_per_spread, 2)
        taken.append((closing.closed_ms, pnl))
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
            "max_loss_per_spread": round(risk_per_spread + entry_costs, 6),
            "equity_at_entry": round(equity, 2),
            "contracts": contracts,
            "settlement": spread.settlement,
            "exit": closing.reason,
            "exit_time": _et_iso(closing.closed_ms),
            "closed": _session_of(closing.closed_ms).isoformat(),
            "exit_debit": round(closing.debit, 6),
            "pnl": pnl,
        })
    return trades, skipped


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def daily_returns(trades, sessions, initial_equity):
    """{day: return} over every session and trade-close day: the P&L trades
    realize that day over the equity before it."""
    pnl_by_day = defaultdict(float)
    for t in trades:
        pnl_by_day[date.fromisoformat(t["closed"])] += t["pnl"]
    returns, equity = {}, initial_equity
    for day in sorted(set(sessions) | set(pnl_by_day)):
        pnl = pnl_by_day.get(day, 0.0)
        returns[day] = pnl / equity
        equity += pnl
    return returns


def compute_metrics(trades, sessions, initial_equity):
    """Risk/return summary over daily returns realized when trades close.

    As in empyrical/quantstats: Sharpe and Sortino are annualized with a zero
    risk-free rate; max drawdown, worst week and CVaR 5% are fractions of
    equity, CVaR being the mean of the worst 5% of daily returns. Win rate and
    average P&L are per trade, the average and the total in dollars."""
    pnls = [t["pnl"] for t in trades]
    metrics = {
        "trades": len(trades),
        "sharpe": None, "sortino": None, "max_drawdown": None,
        "worst_week": None, "cvar_5": None, "win_rate": None,
        "avg_pnl_per_trade": None, "total_pnl": 0.0,
    }
    if not trades:
        return metrics
    by_day = daily_returns(trades, sessions, initial_equity)
    equity = peak = initial_equity
    returns, drawdown = list(by_day.values()), 0.0
    week_open, week_close = {}, {}  # ISO week -> equity before / after it
    for day, daily in by_day.items():
        week = day.isocalendar()[:2]
        week_open.setdefault(week, equity)
        equity *= 1 + daily
        week_close[week] = equity
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    mean = statistics.fmean(returns)
    if len(returns) > 1 and statistics.stdev(returns) > 0:
        metrics["sharpe"] = mean / statistics.stdev(returns) * math.sqrt(TRADING_DAYS)
    downside = math.sqrt(statistics.fmean(min(r, 0.0) ** 2 for r in returns))
    if downside > 0:
        metrics["sortino"] = mean / downside * math.sqrt(TRADING_DAYS)
    tail = sorted(returns)[:max(1, math.ceil(CVAR_TAIL * len(returns)))]
    metrics.update({
        "max_drawdown": drawdown,
        "worst_week": min(week_close[w] / week_open[w] - 1 for w in week_open),
        "cvar_5": statistics.fmean(tail),
        "win_rate": sum(p > 0 for p in pnls) / len(pnls),
        "avg_pnl_per_trade": statistics.fmean(pnls),
        "total_pnl": round(sum(pnls), 2),
    })
    return metrics


# ---------------------------------------------------------------------------
# The experiment run
# ---------------------------------------------------------------------------
def select_candidates(sessions, closes, config):
    """(candidates, skips) over every session of a loaded store."""
    candidates, skipped = [], []
    for session in sorted(sessions):
        candidate, reason = select_candidate(session, sessions[session], closes, config)
        if candidate is None:
            skipped.append(_skip(session, reason))
        else:
            candidates.append(candidate)
    return candidates, skipped


def _report(sessions, marks, priced, skipped, config):
    positions = [Position(spread, find_exit(spread, marks, config))
                 for spread in priced]
    trades, sizing_skips = size_trades(positions, config)
    return {
        "config": asdict(config),
        "sessions": len(sessions),
        "metrics": compute_metrics(trades, sessions, config.initial_equity),
        "trades": trades,
        "skipped": sorted(skipped + sizing_skips, key=lambda skip: skip["session"]),
    }


def run_experiment(store_path, config, start=None, end=None):
    """Replay one configuration over the chain store; return the report dict.
    start/end are inclusive session dates."""
    sessions, closes = load_store(store_path, start, end)
    candidates, skipped = select_candidates(sessions, closes, config)
    priced, pricing_skips = price_candidates(candidates, config.fill_ratio)
    return _report(sessions, leg_marks(sessions), priced, skipped + pricing_skips,
                   config)


def run_grid(store_path, start=None, end=None,
             initial_equity=SpreadConfig.initial_equity,
             risk_pct=SpreadConfig.risk_pct):
    """Replay every grid cell at every fill level; return the grid report.

    Strikes are chosen once per (short delta, width) and priced once per fill
    level; only the exits differ between a cell's hold and managed runs."""
    sessions, closes = load_store(store_path, start, end)
    marks = leg_marks(sessions)
    base = SpreadConfig(initial_equity=initial_equity, risk_pct=risk_pct)
    headline, sensitivity = [], []
    for short_delta, width in product(GRID_SHORT_DELTAS, GRID_WIDTHS):
        cell = replace(base, short_delta=short_delta, width=width)
        candidates, skipped = select_candidates(sessions, closes, cell)
        for fill, fill_ratio in FILL_LEVELS:
            priced, pricing_skips = price_candidates(candidates, fill_ratio)
            for exit_rule in EXIT_RULES:
                config = replace(cell, fill_ratio=fill_ratio, exit_rule=exit_rule)
                report = _report(sessions, marks, priced, skipped + pricing_skips,
                                 config)
                row = {"short_delta": short_delta, "width": width,
                       "exit_rule": exit_rule, "fill": fill, "fill_ratio": fill_ratio,
                       **report["metrics"], "skipped": len(report["skipped"])}
                sensitivity.append(row)
                if fill == HEADLINE_FILL:
                    headline.append(row)
    return {
        "grid": {"short_deltas": list(GRID_SHORT_DELTAS), "widths": list(GRID_WIDTHS),
                 "exit_rules": list(EXIT_RULES), "fills": dict(FILL_LEVELS),
                 "headline_fill": HEADLINE_FILL},
        "initial_equity": initial_equity,
        "risk_pct": risk_pct,
        "sessions": len(sessions),
        "headline": headline,
        "fill_sensitivity": sensitivity,
    }


# ---------------------------------------------------------------------------
# The GEX Filter
# ---------------------------------------------------------------------------
def naive_gex(rows, session):
    """Naive GEX at a session's entry snapshot, or None without one.

    The dashboard's number: canonical-engine gamma over the whole SPX/SPXW
    chain (0DTE included), SOD Open Interest, calls + and puts -. Only the
    snapshot's spot and IV feed the gamma, never vendor greeks. Like the
    dashboard, aggregate_strikes drops contracts with SOD Open Interest
    under its min_oi (10)."""
    stamp = entry_snapshot(rows, session)
    if stamp is None:
        return None
    snapshot = [r for r in rows if r.snapshot_ms == stamp
                and date.fromisoformat(r.expiry) >= session]
    spots = [r.underlying for r in snapshot if r.underlying]
    if not spots:
        return None
    options = [{"t": r.right, "k": r.strike, "oi": r.sod_oi, "iv": r.iv,
                "exp": r.expiry, "root": r.root} for r in snapshot]
    strikes = matrix_gex.aggregate_strikes(options, statistics.median(spots),
                                           CONTRACT_MULTIPLIER, stamp)
    return sum(strike["net_gex"] for strike in strikes)


def gex_series(store_path, first, last):
    """{session: Naive GEX} for every day in [first, last] with an entry
    snapshot. Reads each day's 09:30-10:00 window only, across both roots."""
    connection = chain_store.connect(store_path)
    series = {}
    try:
        day = first
        while day <= last:
            rows = chain_store.read_rows(connection, None, _et_ms(day, *MARKET_OPEN),
                                         _et_ms(day, *ENTRY_TIME))
            value = naive_gex(rows, day) if rows else None
            if value is not None:
                series[day] = value
            day += timedelta(days=1)
    finally:
        connection.close()
    return series


def gamma_regime(value):
    return GAMMA_POSITIVE if value > 0 else GAMMA_NEGATIVE


def _filter_row(label, days, sessions, marks, priced, skipped, config):
    """One report row: the Baseline replayed on `days` only. Metrics keep
    every session's calendar, so rows compare day for day."""
    report = _report(sessions, marks,
                     [spread for spread in priced if spread.session in days],
                     [skip for skip in skipped
                      if date.fromisoformat(skip["session"]) in days], config)
    return {"sessions": label, "session_count": len(days),
            "metrics": report["metrics"], "trades": report["trades"],
            "skipped": report["skipped"]}


def gex_percentiles(series):
    """{session: GEX Percentile or None}: the share of the previous
    GEX_PERCENTILE_LOOKBACK sessions whose Naive GEX sits below the session's
    own. The session itself and anything after it never count; with fewer than
    GEX_PERCENTILE_MIN_HISTORY past sessions there is no percentile."""
    days = sorted(series)
    percentiles = {}
    for i, day in enumerate(days):
        past = [series[d] for d in days[max(0, i - GEX_PERCENTILE_LOOKBACK):i]]
        percentiles[day] = (sum(value < series[day] for value in past) / len(past)
                            if len(past) >= GEX_PERCENTILE_MIN_HISTORY else None)
    return percentiles


# ---------------------------------------------------------------------------
# The IV-Matched Control, the regression, the bootstrap and the verdict
# ---------------------------------------------------------------------------
def _closes_before(closes, session, count):
    """The last `count` closes strictly before session, oldest first, or
    None when there are fewer."""
    days = sorted(closes)
    end = bisect.bisect_left(days, session)
    if end < count:
        return None
    return [closes[day] for day in days[end - count:end]]


def atm_iv(rows, session):
    """IV at the strike nearest spot, on the expiry nearest TARGET_DTE, at the
    session's entry snapshot (the mean of call and put), or None."""
    stamp = entry_snapshot(rows, session)
    if stamp is None:
        return None
    snapshot = [r for r in rows if r.snapshot_ms == stamp and r.underlying
                and matrix_gex.norm_iv(float(r.iv or 0)) > 0
                and date.fromisoformat(r.expiry) > session]
    if not snapshot:
        return None
    expiry = min({r.expiry for r in snapshot}, key=lambda e: abs(
        (date.fromisoformat(e) - session).days - TARGET_DTE))
    chain = [r for r in snapshot if r.expiry == expiry]
    spot = statistics.median(r.underlying for r in chain)
    strike = min({r.strike for r in chain}, key=lambda k: abs(k - spot))
    return statistics.fmean(matrix_gex.norm_iv(float(r.iv))
                            for r in chain if r.strike == strike)


def session_covariates(sessions, spx_closes, vix_closes):
    """{session: {"vix", "atm_iv", "vrp"}}, each None when its input is
    missing. All are known at the 10:00 entry: VIX is the last close before
    the session; ATM IV comes from the entry snapshot; VRP is ATM IV minus the
    annualized realized vol of the last REALIZED_VOL_WINDOW SPX daily
    returns before the session."""
    covariates = {}
    for session, rows in sessions.items():
        vix = _closes_before(vix_closes, session, 1)
        iv = atm_iv(rows, session)
        closes = _closes_before(spx_closes, session, REALIZED_VOL_WINDOW + 1)
        vrp = None
        if iv is not None and closes is not None:
            returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
            vrp = iv - statistics.stdev(returns) * math.sqrt(TRADING_DAYS)
        covariates[session] = {"vix": None if vix is None else vix[0],
                               "atm_iv": iv, "vrp": vrp}
    return covariates


def iv_matched_sessions(decided, count, covariates):
    """(sessions, VIX threshold): the `count` lowest-VIX sessions among
    `decided`, ties going to the earlier session, and the highest VIX they
    include."""
    chosen = sorted(decided, key=lambda day: (covariates[day]["vix"], day))[:count]
    return set(chosen), max((covariates[day]["vix"] for day in chosen), default=None)


def _interval(values):
    values = np.asarray(values, dtype=float)
    values = values[~np.isnan(values)]
    if not len(values):
        return None
    tail = (1 - CONFIDENCE) / 2 * 100
    return [float(np.percentile(values, tail)), float(np.percentile(values, 100 - tail))]


def regression(trades, kept, covariates):
    """OLS of trade P&L on the filter flag (1 = a kept session) with VIX, ATM
    IV and VRP as covariates, over the trades whose covariates are all known.
    The filter coefficient's interval uses Student's t; it is None when the
    flag is not identified (a constant, or a linear function of the
    covariates)."""
    names = ("intercept", "filter") + REGRESSION_COVARIATES
    X, y = [], []
    for trade in trades:
        day = date.fromisoformat(trade["session"])
        values = [covariates[day][name] for name in REGRESSION_COVARIATES]
        if None not in values:
            X.append([1.0, float(day in kept), *values])
            y.append(trade["pnl"])
    result = {"trades": len(y), "dropped": len(trades) - len(y),
              "coefficients": None, "filter_coef": None, "filter_se": None,
              "filter_ci": None}
    if not y:
        return result
    X, y = np.array(X), np.array(y)
    rank = np.linalg.matrix_rank(X)
    dof = len(y) - rank
    if dof < 1 or np.linalg.matrix_rank(np.delete(X, 1, axis=1)) == rank:
        return result
    beta = np.linalg.pinv(X) @ y
    residuals = y - X @ beta
    # pinv keeps the flag's variance exact when covariates are collinear.
    covariance = residuals @ residuals / dof * np.linalg.pinv(X.T @ X)
    se = math.sqrt(max(covariance[1, 1], 0.0))
    half = stats.t.ppf(0.5 + CONFIDENCE / 2, dof) * se
    result.update({
        "coefficients": dict(zip(names, map(float, beta))),
        "filter_coef": float(beta[1]), "filter_se": se,
        "filter_ci": [float(beta[1] - half), float(beta[1] + half)],
    })
    return result


def _sharpe(returns):
    """Annualized Sharpe of each row of `returns`; NaN where flat."""
    std = returns.std(axis=-1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = returns.mean(axis=-1) / std
    return np.where(std > 0, ratio, np.nan) * math.sqrt(TRADING_DAYS)


def bootstrap(filter_trades, control_trades, sessions, initial_equity):
    """Paired moving-block bootstrap of filter minus IV-Matched Control:
    Sharpe over daily returns, and mean P&L per trade (trades follow their
    entry day). Both strategies resample the same days."""
    by_filter = daily_returns(filter_trades, sessions, initial_equity)
    by_control = daily_returns(control_trades, sessions, initial_equity)
    calendar = sorted(set(by_filter) | set(by_control))
    result = {"resamples": BOOTSTRAP_RESAMPLES, "block": BOOTSTRAP_BLOCK,
              "sharpe": {"difference": None, "ci": None},
              "mean_pnl": {"difference": None, "ci": None}}
    if len(calendar) < 2:
        return result
    index = {day: i for i, day in enumerate(calendar)}
    returns = np.array([[by_filter.get(day, 0.0) for day in calendar],
                        [by_control.get(day, 0.0) for day in calendar]])
    pnl, count = np.zeros((2, len(calendar))), np.zeros((2, len(calendar)))
    for side, trades in enumerate((filter_trades, control_trades)):
        for trade in trades:
            i = index[date.fromisoformat(trade["session"])]
            pnl[side, i] += trade["pnl"]
            count[side, i] += 1

    def mean_pnl(days):
        with np.errstate(divide="ignore", invalid="ignore"):
            means = pnl[:, days].sum(axis=-1) / count[:, days].sum(axis=-1)
        return means[0] - means[1]

    def sharpe(days):
        return _sharpe(returns[0][days]) - _sharpe(returns[1][days])

    n = len(calendar)
    block = min(BOOTSTRAP_BLOCK, n)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    starts = rng.integers(0, n - block + 1,
                          size=(BOOTSTRAP_RESAMPLES, math.ceil(n / block)))
    resampled = (starts[:, :, None] + np.arange(block)).reshape(
        BOOTSTRAP_RESAMPLES, -1)[:, :n]
    for name, measure in (("sharpe", sharpe), ("mean_pnl", mean_pnl)):
        point = float(measure(np.arange(n)))
        result[name] = {"difference": None if math.isnan(point) else point,
                        "ci": _interval(measure(resampled))}
    return result


def _mean_pnl(trades):
    return statistics.fmean(t["pnl"] for t in trades) if trades else None


def by_year(filter_trades, control_trades):
    """Mean P&L per trade for the filter and its control, by entry year."""
    def year_of(trade):
        return date.fromisoformat(trade["session"]).year

    rows = []
    for year in sorted({year_of(t) for t in filter_trades + control_trades}):
        kept = [t for t in filter_trades if year_of(t) == year]
        control = [t for t in control_trades if year_of(t) == year]
        rows.append({"year": year, "filter_trades": len(kept),
                     "filter_mean_pnl": _mean_pnl(kept),
                     "control_trades": len(control),
                     "control_mean_pnl": _mean_pnl(control)})
    return rows


def compare(kept, rows, covariates, sessions, config):
    """A filter against its IV-Matched Control. The verdict is positive only
    when the regression's filter interval and both bootstrap intervals sit
    above zero, and the filter's mean P&L per trade beats the control's in
    every year both trade."""
    filter_trades = rows[ROW_KEPT]["trades"]
    control_trades = rows[ROW_IV_MATCHED]["trades"]
    fit = regression(rows[ROW_BASELINE]["trades"], kept, covariates)
    resampled = bootstrap(filter_trades, control_trades, sessions,
                          config.initial_equity)
    years = by_year(filter_trades, control_trades)
    significant = all(ci is not None and ci[0] > 0 for ci in (
        fit["filter_ci"], resampled["sharpe"]["ci"], resampled["mean_pnl"]["ci"]))
    compared = [row for row in years if row["filter_mean_pnl"] is not None
                and row["control_mean_pnl"] is not None]
    consistent = bool(compared) and all(
        row["filter_mean_pnl"] > row["control_mean_pnl"] for row in compared)
    return {"verdict": VERDICT_POSITIVE if significant and consistent else VERDICT_NULL,
            "significant": significant, "consistent_across_years": consistent,
            "regression": fit, "bootstrap": resampled, "by_year": years}


def verdict_line(filters):
    winners = [f["filter"] for f in filters
               if f["comparison"]["verdict"] == VERDICT_POSITIVE]
    return VERDICT_POSITIVE_LINE.format(", ".join(winners)) if winners else VERDICT_NULL_LINE


def run_gex_filter(store_path, config, start=None, end=None,
                   percentile_threshold=GEX_PERCENTILE_THRESHOLD):
    """Replay the Baseline and every GEX Filter over the chain store; return
    the report dict. start/end are inclusive session dates.

    Each filter splits the sessions into kept and Rejected Sessions; both are
    replayed as a strategy of their own, beside the Baseline and the filter's
    IV-Matched Control, and the filter is compared with its control. Its
    Undecided Sessions (no Naive GEX, no GEX Percentile yet, or no VIX close
    before the session) sit outside the comparison, the Baseline row
    included, so kept + rejected = Baseline. GEX Percentile history reaches
    back before start. The report opens with the verdict line."""
    sessions, closes = load_store(store_path, start, end)
    covariates = session_covariates(sessions, closes,
                                    load_closes(store_path, VIX_SYMBOL))
    series = {}
    if sessions:
        series = gex_series(store_path,
                            min(sessions) - timedelta(days=GEX_HISTORY_CALENDAR_DAYS),
                            max(sessions))
    percentiles = gex_percentiles(series)
    candidates, skipped = select_candidates(sessions, closes, config)
    priced, pricing_skips = price_candidates(candidates, config.fill_ratio)
    skipped += pricing_skips
    marks = leg_marks(sessions)

    def positive_gamma(day):
        if day not in series:
            return None, "no Naive GEX at the entry snapshot"
        return series[day] > 0, None

    def above_percentile(day):
        if day not in series:
            return None, "no Naive GEX at the entry snapshot"
        if percentiles[day] is None:
            return None, (f"fewer than {GEX_PERCENTILE_MIN_HISTORY}"
                          " past sessions of Naive GEX")
        return percentiles[day] > percentile_threshold, None

    variants = ((FILTER_POSITIVE_GAMMA, positive_gamma, {}),
                (FILTER_GEX_PERCENTILE, above_percentile,
                 {"threshold": percentile_threshold}))
    filters = []
    for name, decide, settings in variants:
        kept, rejected, undecided = set(), set(), []
        for day in sorted(sessions):
            keep, reason = decide(day)
            if keep is not None and covariates[day]["vix"] is None:
                keep, reason = None, "no VIX close before the session"
            if keep is None:
                undecided.append(_skip(day, reason))
            else:
                (kept if keep else rejected).add(day)
        control, threshold = iv_matched_sessions(kept | rejected, len(kept), covariates)
        rows = {label: _filter_row(label, days, sessions, marks, priced, skipped, config)
                for label, days in ((ROW_KEPT, kept), (ROW_REJECTED, rejected),
                                    (ROW_BASELINE, kept | rejected),
                                    (ROW_IV_MATCHED, control))}
        rows[ROW_IV_MATCHED]["vix_threshold"] = threshold
        filters.append({"filter": name, **settings, "rows": list(rows.values()),
                        "comparison": compare(kept, rows, covariates, sessions, config),
                        "undecided": undecided})
    return {
        "verdict": verdict_line(filters),
        "config": asdict(config),
        "sessions": len(sessions),
        "covariates": [{"session": day.isoformat(), **covariates[day]}
                       for day in sorted(sessions)],
        "gex": [{"session": day.isoformat(), "naive_gex": series[day],
                 "gamma_regime": gamma_regime(series[day]),
                 "gex_percentile": percentiles[day]}
                for day in sorted(series) if day in sessions],
        "filters": filters,
    }


# ---------------------------------------------------------------------------
# The human-readable summary
# ---------------------------------------------------------------------------
def _number(value, digits=2):
    return "-" if value is None else f"{value:,.{digits}f}"


def _range(ci, digits=2):
    return "-" if ci is None else f"[{_number(ci[0], digits)}, {_number(ci[1], digits)}]"


def format_summary(report):
    """The GEX Filter report as Markdown, opening with the verdict line."""
    config = report["config"]
    lines = [report["verdict"], "",
             f"Short delta {config['short_delta']:g}, width {config['width']:g},"
             f" exit {config['exit_rule']}, fill ratio {config['fill_ratio']:g};"
             f" {report['sessions']} sessions."]
    for variant in report["filters"]:
        comparison = variant["comparison"]
        lines += ["", f"## {variant['filter']}", "",
                  f"Verdict: {comparison['verdict']} (significant:"
                  f" {'yes' if comparison['significant'] else 'no'}; consistent across"
                  f" years: {'yes' if comparison['consistent_across_years'] else 'no'})",
                  "", "| Sessions | Count | Trades | Total P&L | Avg P&L | Win rate"
                  " | Sharpe | Max drawdown |", "|---|---|---|---|---|---|---|---|"]
        for row in variant["rows"]:
            metrics, label = row["metrics"], row["sessions"]
            if row.get("vix_threshold") is not None:
                label += f" (VIX <= {row['vix_threshold']:.2f})"
            lines.append(
                f"| {label} | {row['session_count']} | {metrics['trades']}"
                f" | {_number(metrics['total_pnl'])} | {_number(metrics['avg_pnl_per_trade'])}"
                f" | {_number(metrics['win_rate'], 3)} | {_number(metrics['sharpe'])}"
                f" | {_number(metrics['max_drawdown'], 4)} |")
        fit, resampled = comparison["regression"], comparison["bootstrap"]
        lines += ["",
                  f"Regression over {fit['trades']} trades ({fit['dropped']} without"
                  f" covariates): filter coefficient {_number(fit['filter_coef'])}"
                  f" {_range(fit['filter_ci'])}.",
                  f"Bootstrap, filter minus IV-Matched Control: Sharpe"
                  f" {_number(resampled['sharpe']['difference'])}"
                  f" {_range(resampled['sharpe']['ci'])}; mean P&L per trade"
                  f" {_number(resampled['mean_pnl']['difference'])}"
                  f" {_range(resampled['mean_pnl']['ci'])}.",
                  "", "| Year | Filter trades | Filter avg P&L | Control trades"
                  " | Control avg P&L |", "|---|---|---|---|---|"]
        lines += [f"| {row['year']} | {row['filter_trades']}"
                  f" | {_number(row['filter_mean_pnl'])} | {row['control_trades']}"
                  f" | {_number(row['control_mean_pnl'])} |"
                  for row in comparison["by_year"]]
    return "\n".join(lines) + "\n"


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
    # The per-cell flags default to None so --grid can reject them.
    parser.add_argument("--short-delta", type=float,
                        help=f"Short put delta (default {SpreadConfig.short_delta})")
    parser.add_argument("--width", type=float,
                        help=f"Strike width in points (default {SpreadConfig.width:g})")
    parser.add_argument("--equity", type=float, default=SpreadConfig.initial_equity)
    parser.add_argument("--risk-pct", type=float, default=SpreadConfig.risk_pct)
    parser.add_argument("--fill-ratio", type=float,
                        help="Share of the half-spread crossed from mid, 0..1 "
                             f"(default {SpreadConfig.fill_ratio})")
    parser.add_argument("--exit-rule", choices=EXIT_RULES,
                        help="Hold to expiry, or 50%% take-profit / 2x credit stop "
                             f"(default {SpreadConfig.exit_rule})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--grid", action="store_true",
                      help="Run the whole parameter grid at every fill level")
    mode.add_argument("--gex-filter", action="store_true",
                      help="Report every GEX Filter's kept and Rejected Sessions"
                           " beside the Baseline")
    parser.add_argument("--gex-percentile", type=float,
                        help="GEX Percentile the percentile filter must exceed, 0..1 "
                             f"(default {GEX_PERCENTILE_THRESHOLD}; --gex-filter only)")
    parser.add_argument("--out", help="Also write the JSON report to this path")
    parser.add_argument("--summary",
                        help="Also write a human-readable summary to this path"
                             " (--gex-filter only)")
    args = parser.parse_args(argv)
    cell_flags = {"--short-delta": args.short_delta, "--width": args.width,
                  "--fill-ratio": args.fill_ratio, "--exit-rule": args.exit_rule}
    if args.grid and any(value is not None for value in cell_flags.values()):
        parser.error("--grid runs every cell; drop "
                     + ", ".join(flag for flag, value in cell_flags.items()
                                 if value is not None))
    if args.gex_percentile is not None and not args.gex_filter:
        parser.error("--gex-percentile needs --gex-filter")
    if args.summary is not None and not args.gex_filter:
        parser.error("--summary needs --gex-filter")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.grid:
            report = run_grid(args.store, args.start, args.end,
                              initial_equity=args.equity, risk_pct=args.risk_pct)
        else:
            cell = {"short_delta": args.short_delta, "width": args.width,
                    "fill_ratio": args.fill_ratio, "exit_rule": args.exit_rule}
            config = SpreadConfig(initial_equity=args.equity, risk_pct=args.risk_pct,
                                  **{k: v for k, v in cell.items() if v is not None})
            if args.gex_filter:
                threshold = (GEX_PERCENTILE_THRESHOLD if args.gex_percentile is None
                             else args.gex_percentile)
                report = run_gex_filter(args.store, config, args.start, args.end,
                                        percentile_threshold=threshold)
            else:
                report = run_experiment(args.store, config, args.start, args.end)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"backtest_spread: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    if args.summary:
        Path(args.summary).write_text(format_summary(report), encoding="utf-8")
    print(text)
    if args.grid:
        return 0 if any(row["trades"] for row in report["headline"]) else 1
    if args.gex_filter:
        return 0 if report["gex"] else 1
    return 0 if report["trades"] else 1


if __name__ == "__main__":
    sys.exit(main())
