"""Backtest four option strategies on SPY and QQQ over the chain store.

tools/backtest_spread.py replays one strategy (the SPXW put credit spread
Baseline). This one compares several, on the ETF underlyings the trader
actually trades, before any strategy is chosen:

- put_credit_spread: sell the put nearest -delta, buy the put `width` below;
- iron_condor: that spread plus the mirror call spread above;
- short_strangle: sell the put nearest -delta and the call nearest +delta;
- long_strangle: buy the put nearest -delta and the call nearest +delta.

Every session at 10:00 ET one position is opened on the expiry nearest seven
calendar days out (ties go to the earlier expiry; never the same day). Deltas
come from the canonical greeks engine and the snapshot IV, as in the spread
backtester. A wing is the strike nearest `width` beyond its short strike,
within WING_TOLERANCE dollars.

Exits:
- "hold": settled at the underlying's close on the expiry date. ETF options
  settle in shares; the backtest books that as closing every leg at its
  intrinsic value against the close, with no costs (no overnight gap);
- "managed": closed at the first intraday snapshot that hits a target. A
  credit position takes profit when buying it back costs <= 50% of the credit
  and stops at >= 2x the credit; a debit position (the long strangle) takes
  profit when selling it brings >= 2x the debit and stops at <= half of it.
  A position that hits neither is settled as under "hold".
- Early assignment, under both rules: ETF options are American. A short call
  that is in the money at the close of the trading day before an ex-dividend
  date, with time value (its mid less intrinsic, against that close) below
  the dividend, is assumed exercised that evening. The position closes at
  that day's last snapshot: the assigned call at intrinsic, the other legs at
  their fills (or intrinsic when unquoted). Early exercise of deep in-the-money
  puts is not modelled.

Fills, costs and sizing:
- fills cross fill_ratio of the half-spread from mid, on every leg, on entry
  and managed exits alike (0 = mid, 0.5 = default, 1 = the far side);
- IBKR tiered commissions with the per-leg order minimum, plus an exchange
  and regulatory fee per contract: Cboe's SPXW schedule for SPX, and the
  rough ETF_FEE_PER_CONTRACT estimate for SPY/QQQ (check it against an IBKR
  statement before trusting small-premium results);
- at most five positions open at once;
- contracts sized so the Risk Unit times contracts, plus entry costs, is at
  most risk_pct of equity. The Risk Unit is the worst expiry loss over every
  strike of the position and a STRESS_MOVE gap either way from spot: the
  exact max loss of a defined-risk position, and a 10% gap loss for the short
  strangle, whose loss is otherwise unbounded.

The Holdout: sessions from HOLDOUT_START on are never replayed unless
--holdout is passed, and then only they are. Choose a strategy on the Dev
Period first; run the Holdout once, on that one choice.

--compare runs every strategy over its parameter grid (delta, width, exit
rule) at one fill level per underlying, beside buy-and-hold of the
underlying. The more configurations a comparison tries, the better its best
one looks by luck alone: the report states how many it tried.

Usage:
    python tools/backtest_options.py --store PATH --underlying SPY
        --strategy iron_condor [--delta 0.16] [--width 5] [--exit-rule managed]
        [--fill-ratio 0.5] [--equity 1000000] [--risk-pct 0.01]
        [--start DATE] [--end DATE] [--holdout] [--out report.json]
    python tools/backtest_options.py --store PATH --compare
        [--underlying SPY,QQQ] [--fill-ratio 0.5] [--start DATE] [--end DATE]
        [--holdout] [--out compare.json] [--summary compare.md]

The JSON report is printed to stdout (and written to --out when given).
"""
import argparse
import bisect
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tripity_experiment import chain_store
from tripity_experiment import matrix_gex
import backtest_spread as spread_bt

ET = matrix_gex.ET

# --- Strategies ---
PUT_CREDIT_SPREAD = "put_credit_spread"
IRON_CONDOR = "iron_condor"
SHORT_STRANGLE = "short_strangle"
LONG_STRANGLE = "long_strangle"
STRATEGIES = (PUT_CREDIT_SPREAD, IRON_CONDOR, SHORT_STRANGLE, LONG_STRANGLE)
WINGED = (PUT_CREDIT_SPREAD, IRON_CONDOR)  # the strategies that take a width
LONG, SHORT = 1, -1

# --- Entry ---
TARGET_DTE = 7  # calendar days
ENTRY_TIME = (10, 0)  # ET
MARKET_OPEN = spread_bt.MARKET_OPEN
MARKET_CLOSE = spread_bt.MARKET_CLOSE
ENTRY_MAX_AGE_MINUTES = 45  # an older entry snapshot is skipped, not traded
WING_TOLERANCE = 1.0  # dollars a wing strike may sit from short +/- width
ROOT_OF = {"SPX": "SPXW"}  # the root traded for an underlying; else itself

# --- Costs ---
CONTRACT_MULTIPLIER = 100
COMMISSION_TIERS = spread_bt.COMMISSION_TIERS
MIN_COMMISSION_PER_LEG = spread_bt.MIN_COMMISSION_PER_LEG
# Exchange, ORF and OCC fees IBKR passes through on an ETF option, per
# contract. They vary by venue and by adding or taking liquidity; this is a
# deliberately rough, slightly conservative figure.
ETF_FEE_PER_CONTRACT = 0.25
FEE_TIERS = {"SPX": spread_bt.EXCHANGE_FEE_TIERS}
ETF_FEE_TIERS = ((0.0, ETF_FEE_PER_CONTRACT),)

# --- Position management ---
RULE_HOLD = spread_bt.RULE_HOLD
RULE_MANAGED = spread_bt.RULE_MANAGED
EXIT_RULES = (RULE_HOLD, RULE_MANAGED)
TAKE_PROFIT_SHARE = 0.5  # credit: buy back at <= this share of the credit
STOP_MULTIPLE = 2.0  # credit: buy back at >= this multiple of the credit
LONG_TAKE_PROFIT_MULTIPLE = 2.0  # debit: sell at >= this multiple of the debit
LONG_STOP_SHARE = 0.5  # debit: sell at <= this share of the debit
CLOSED_TAKE_PROFIT = spread_bt.CLOSED_TAKE_PROFIT
CLOSED_STOP = spread_bt.CLOSED_STOP
CLOSED_EXPIRY = spread_bt.CLOSED_EXPIRY
CLOSED_ASSIGNED = "assigned"
MAX_OPEN_POSITIONS = 5
STRESS_MOVE = 0.10  # gap either way behind the Risk Unit

# --- The Holdout ---
HOLDOUT_START = date(2025, 10, 1)

# --- The comparison grid ---
CREDIT_DELTAS = (0.10, 0.16, 0.20)
LONG_DELTAS = (0.25, 0.40)
ETF_WIDTHS = (3.0, 5.0)
DEFAULT_WIDTH = 5.0
TRADING_DAYS = spread_bt.TRADING_DAYS
MULTIPLE_TESTING_LINE = (
    "{} configurations were tried. With that many, the best ones look good"
    " partly by luck: pick on the Dev Period, then run that one on the Holdout.")
CAVEATS = (
    f"ETF exchange fees are a rough ${ETF_FEE_PER_CONTRACT:.2f} per contract estimate.",
    "IV and deltas are European (Black-Scholes) on the parity level;"
    " ETF options are American.",
    "Expiry is booked at intrinsic against the close: no overnight gap after"
    " share delivery.",
    "Early assignment is modelled for short calls before ex-dividend dates only.",
    "Buy-and-hold is price return, dividends excluded.",
)


@dataclass(frozen=True)
class OptionsConfig:
    underlying: str = "SPY"
    strategy: str = PUT_CREDIT_SPREAD
    delta: float = 0.16
    width: float | None = DEFAULT_WIDTH  # None for the strangles
    fill_ratio: float = 0.5
    exit_rule: str = RULE_HOLD
    initial_equity: float = 1_000_000.0
    risk_pct: float = 0.01

    def __post_init__(self):
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}")
        if self.exit_rule not in EXIT_RULES:
            raise ValueError(f"unknown exit rule {self.exit_rule!r}")
        if (self.strategy in WINGED) != (self.width is not None):
            raise ValueError(f"{self.strategy} {'needs' if self.strategy in WINGED else 'takes no'}"
                             " width")
        if not 0 < self.delta < 1:
            raise ValueError("delta is a magnitude between 0 and 1")

    @property
    def selection(self):
        """What picks the strikes; configurations sharing it trade the same legs."""
        return (self.strategy, self.delta, self.width)


@dataclass(frozen=True)
class Leg:
    row: chain_store.ChainRow
    side: int  # LONG or SHORT


@dataclass(frozen=True)
class Candidate:
    """The position one session would open, before fills."""
    session: date
    expiry: date
    legs: tuple
    entry_ms: int
    spot: float  # the parity level at entry
    settlement: float  # the underlying's close on the expiry date


@dataclass(frozen=True)
class Exit:
    """How a position closes, per share. fills[i] is leg i's closing fill, or
    None when it settled (or was assigned) at intrinsic, which costs nothing."""
    reason: str
    closed_ms: int
    cash: float  # received to close; negative when paid
    fills: tuple


@dataclass(frozen=True)
class Position:
    candidate: Candidate
    entry_fills: tuple
    entry_cash: float  # received to open, per share; negative for a debit
    exit: Exit


def _skip(session, reason):
    return {"session": session.isoformat(), "reason": reason}


def intrinsic(right, strike, price):
    return max(price - strike, 0.0) if right == "C" else max(strike - price, 0.0)


def _mid(row):
    return (row.bid + row.ask) / 2


def _fill(row, side, opening, fill_ratio):
    """The fill of one leg: mid moved fill_ratio of the half-spread against us.
    Opening buys the long legs and sells the short ones; closing reverses."""
    buying = (side == LONG) == opening
    half = (row.ask - row.bid) / 2 * fill_ratio
    return _mid(row) + half if buying else _mid(row) - half


def _opening_cash(legs, fills):
    return sum(-leg.side * fill for leg, fill in zip(legs, fills))


def _closing_cash(legs, fills):
    return sum(leg.side * fill for leg, fill in zip(legs, fills))


# ---------------------------------------------------------------------------
# The chain store, read lazily: a full SPY history does not fit in memory
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, path, underlying):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"chain store not found: {path}")
        self.underlying = underlying
        self.root = ROOT_OF.get(underlying, underlying)
        self.connection = chain_store.connect(path)
        self.closes = {date.fromisoformat(d): c for d, c in
                       chain_store.read_daily_closes(self.connection, underlying).items()}
        self.close_days = sorted(self.closes)
        self.dividends = {date.fromisoformat(d): a for d, a in
                          chain_store.read_dividends(self.connection, underlying).items()}
        self._marks = {}

    def close(self):
        self.connection.close()

    def sessions(self, first=None, last=None):
        """Every session the root has chain rows for, in [first, last]."""
        start_ms = spread_bt._et_ms(first, 0, 0) if first else 0
        end_ms = spread_bt._et_ms(last, 23, 59) if last else 2**62
        days = sorted({spread_bt._session_of(ms) for (ms,) in self.connection.execute(
            "SELECT DISTINCT snapshot_ms FROM option_chain_snapshot"
            " WHERE snapshot_ms BETWEEN ? AND ?", (start_ms, end_ms))})
        return [day for day in days if self.connection.execute(
            "SELECT 1 FROM option_chain_snapshot WHERE snapshot_ms BETWEEN ? AND ?"
            " AND root = ? LIMIT 1",
            (spread_bt._et_ms(day, 0, 0), spread_bt._et_ms(day, 23, 59), self.root)
        ).fetchone()]

    def entry_chain(self, session):
        """(EntryChain, None) or (None, skip reason) for one session."""
        entry_ms = spread_bt._et_ms(session, *ENTRY_TIME)
        open_ms = spread_bt._et_ms(session, *MARKET_OPEN)
        stamps = [ms for (ms,) in self.connection.execute(
            "SELECT DISTINCT snapshot_ms FROM option_chain_snapshot"
            " WHERE snapshot_ms BETWEEN ? AND ? AND root = ?",
            (open_ms, entry_ms, self.root))]
        if not stamps:
            return None, "no snapshot at/before entry time"
        stamp = max(stamps)
        if entry_ms - stamp > ENTRY_MAX_AGE_MINUTES * 60 * 1000:
            return None, "no snapshot at/before entry time"
        return EntryChain(chain_store.read_rows(self.connection, self.root, stamp, stamp)), None

    def marks(self, row):
        """{snapshot_ms: ChainRow} over the contract's whole stored life."""
        key = (row.root, row.expiry, row.strike, row.right)
        if key not in self._marks:
            columns = ", ".join(chain_store.CHAIN_COLUMNS)
            self._marks[key] = {r.snapshot_ms: r for r in (
                chain_store.ChainRow(*values) for values in self.connection.execute(
                    f"SELECT {columns} FROM option_chain_snapshot WHERE root = ?"
                    " AND expiry = ? AND strike = ? AND right = ?", key))}
        return self._marks[key]

    def trading_day_before(self, day):
        index = bisect.bisect_left(self.close_days, day)
        return self.close_days[index - 1] if index else None


class EntryChain:
    """One entry snapshot, indexed by expiry and right, with lazy deltas."""

    def __init__(self, rows):
        self.stamp = rows[0].snapshot_ms if rows else None
        self.by_expiry = defaultdict(lambda: {"P": {}, "C": {}})
        for row in rows:
            self.by_expiry[date.fromisoformat(row.expiry)][row.right][row.strike] = row
        self._deltas = {}

    def spot(self):
        return next((row.underlying for chain in self.by_expiry.values()
                     for side in chain.values() for row in side.values()
                     if row.underlying), None)

    def delta(self, row):
        key = (row.expiry, row.right, row.strike)
        if key not in self._deltas:
            sigma = matrix_gex.norm_iv(float(row.iv or 0))
            self._deltas[key] = None if sigma <= 0 or not row.underlying else matrix_gex.bs_delta(
                row.underlying, row.strike,
                matrix_gex.years_to_expiry(row.expiry, row.root, row.snapshot_ms),
                sigma, is_call=row.right == "C")
        return self._deltas[key]

    def at_delta(self, expiry, right, target):
        """The row whose delta is nearest +target (call) or -target (put)."""
        signed = target if right == "C" else -target
        rows = [(abs(d - signed), row.strike, row) for row in self.by_expiry[expiry][right].values()
                if (d := self.delta(row)) is not None]
        return min(rows, key=lambda r: (r[0], r[1]))[2] if rows else None

    def wing(self, expiry, short, width):
        """The strike nearest `width` beyond a short leg, away from the money."""
        direction = -1 if short.right == "P" else 1
        target = short.strike + direction * width
        rows = [row for strike, row in self.by_expiry[expiry][short.right].items()
                if (strike - short.strike) * direction > 0]
        if not rows:
            return None
        row = min(rows, key=lambda r: (abs(r.strike - target), r.strike))
        return row if abs(row.strike - target) <= WING_TOLERANCE else None


# ---------------------------------------------------------------------------
# Trade selection
# ---------------------------------------------------------------------------
def _legs(chain, expiry, selection):
    """(legs, None) or (None, reason) for one strategy on one expiry."""
    strategy, delta, width = selection
    put = chain.at_delta(expiry, "P", delta)
    call = None if strategy == PUT_CREDIT_SPREAD else chain.at_delta(expiry, "C", delta)
    if put is None or (strategy != PUT_CREDIT_SPREAD and call is None):
        return None, "no strike with a usable IV"
    if strategy == LONG_STRANGLE:
        return (Leg(put, LONG), Leg(call, LONG)), None
    if strategy == SHORT_STRANGLE:
        return (Leg(put, SHORT), Leg(call, SHORT)), None
    pairs = [(put, chain.wing(expiry, put, width))]
    if strategy == IRON_CONDOR:
        pairs.append((call, chain.wing(expiry, call, width)))
    legs = []
    for short, wing in pairs:
        if wing is None:
            sign = "-" if short.right == "P" else "+"
            return None, f"no wing strike near {short.strike:g} {sign} {width:g}"
        legs += [Leg(short, SHORT), Leg(wing, LONG)]
    return tuple(legs), None


def select_candidate(session, chain, selection, closes):
    """(Candidate, None) or (None, skip_reason) for one session."""
    expiries = [e for e in chain.by_expiry if e > session]
    if not expiries:
        return None, "no expiry after the session"
    expiry = min(expiries, key=lambda e: (abs((e - session).days - TARGET_DTE), e))
    legs, reason = _legs(chain, expiry, selection)
    if legs is None:
        return None, reason
    if not all(spread_bt._quote_ok(leg.row) for leg in legs):
        return None, "missing or crossed quote on a leg"
    settlement = closes.get(expiry)
    if settlement is None:
        return None, f"no close to settle {expiry.isoformat()}"
    spot = chain.spot()
    if spot is None:
        return None, "no underlying level at entry"
    return Candidate(session=session, expiry=expiry, legs=legs, entry_ms=chain.stamp,
                     spot=spot, settlement=settlement), None


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------
def _managed_reason(entry_cash, exit_cash):
    if entry_cash > 0:  # a credit: what buying it back costs
        cost = -exit_cash
        if cost <= TAKE_PROFIT_SHARE * entry_cash:
            return CLOSED_TAKE_PROFIT
        if cost >= STOP_MULTIPLE * entry_cash:
            return CLOSED_STOP
        return None
    debit = -entry_cash
    if exit_cash >= LONG_TAKE_PROFIT_MULTIPLE * debit:
        return CLOSED_TAKE_PROFIT
    if exit_cash <= LONG_STOP_SHARE * debit:
        return CLOSED_STOP
    return None


def _close_at(legs, stamp, spot, store, fill_ratio, settled=()):
    """(cash, fills) closing every leg at `stamp`: the legs in `settled`, and
    any leg without a usable quote, at intrinsic against spot."""
    cash, fills = 0.0, []
    for index, leg in enumerate(legs):
        row = store.marks(leg.row).get(stamp)
        if index in settled or row is None or not spread_bt._exit_quote_ok(row):
            cash += leg.side * intrinsic(leg.row.right, leg.row.strike, spot)
            fills.append(None)
        else:
            fill = _fill(row, leg.side, opening=False, fill_ratio=fill_ratio)
            cash += leg.side * fill
            fills.append(fill)
    return cash, tuple(fills)


def early_assignment(candidate, store, fill_ratio):
    """The Exit an early assignment forces, or None.

    A short call is assigned on the evening of the trading day before an
    ex-dividend date when, at that day's close, it is in the money and its
    time value is below the dividend."""
    events = []
    for index, leg in enumerate(candidate.legs):
        if leg.side != SHORT or leg.row.right != "C":
            continue
        for ex_date, amount in store.dividends.items():
            if not candidate.session < ex_date <= candidate.expiry:
                continue
            day = store.trading_day_before(ex_date)
            spot = store.closes.get(day) if day else None
            if spot is None or day < candidate.session:
                continue
            marks = store.marks(leg.row)
            stamps = [ms for ms in marks if spread_bt._session_of(ms) == day
                      and spread_bt._during_session(ms) and ms > candidate.entry_ms]
            if not stamps:
                continue
            row = marks[max(stamps)]
            value = intrinsic("C", leg.row.strike, spot)
            if value > 0 and spread_bt._quote_ok(row) and _mid(row) - value < amount:
                events.append((max(stamps), day, index))
    if not events:
        return None
    stamp, day, index = min(events)
    cash, fills = _close_at(candidate.legs, stamp, store.closes[day], store, fill_ratio,
                            settled={index})
    return Exit(reason=CLOSED_ASSIGNED, closed_ms=spread_bt._et_ms(day, *MARKET_CLOSE),
                cash=cash, fills=fills)


def find_exit(candidate, entry_cash, store, config):
    """The Exit the configuration's exit rule takes for one opened position."""
    legs = candidate.legs
    settle = Exit(
        reason=CLOSED_EXPIRY,
        closed_ms=spread_bt._et_ms(candidate.expiry, *MARKET_CLOSE),
        cash=sum(leg.side * intrinsic(leg.row.right, leg.row.strike, candidate.settlement)
                 for leg in legs) + 0.0,
        fills=(None,) * len(legs))
    fallback = early_assignment(candidate, store, config.fill_ratio) or settle
    if config.exit_rule == RULE_HOLD:
        return fallback
    marks = [store.marks(leg.row) for leg in legs]
    for stamp in sorted(set.intersection(*(set(m) for m in marks))):
        if not (candidate.entry_ms < stamp <= fallback.closed_ms
                and spread_bt._during_session(stamp)):
            continue
        rows = [m[stamp] for m in marks]
        if not all(spread_bt._exit_quote_ok(row) for row in rows):
            continue
        fills = tuple(_fill(row, leg.side, opening=False, fill_ratio=config.fill_ratio)
                      for row, leg in zip(rows, legs))
        cash = _closing_cash(legs, fills)
        reason = _managed_reason(entry_cash, cash)
        if reason:
            return Exit(reason=reason, closed_ms=stamp, cash=cash, fills=fills)
    return fallback


# ---------------------------------------------------------------------------
# Costs and sizing
# ---------------------------------------------------------------------------
def fee_tiers(underlying):
    return FEE_TIERS.get(underlying, ETF_FEE_TIERS)


def order_costs(fills, contracts, tiers):
    """Commissions and fees for one order of `contracts` of every filled leg."""
    return sum(max(contracts * spread_bt._tier_rate(COMMISSION_TIERS, p),
                   MIN_COMMISSION_PER_LEG)
               + contracts * spread_bt._tier_rate(tiers, p)
               for p in fills if p is not None)


def risk_unit(candidate, entry_cash):
    """Worst expiry loss of one position per share, over every strike and a
    STRESS_MOVE gap either way from spot."""
    prices = {leg.row.strike for leg in candidate.legs}
    prices |= {candidate.spot * (1 - STRESS_MOVE), candidate.spot * (1 + STRESS_MOVE)}
    worst = max(-(entry_cash + sum(leg.side * intrinsic(leg.row.right, leg.row.strike, p)
                                   for leg in candidate.legs))
                for p in prices)
    return max(worst, 0.0)


def contracts_within_budget(budget, risk, fills, tiers):
    """Most positions whose total Risk Unit, entry costs included, fits budget."""
    floor_cost = sum(spread_bt._tier_rate(COMMISSION_TIERS, p) + spread_bt._tier_rate(tiers, p)
                     for p in fills)
    if risk + floor_cost <= 0:
        return 0
    contracts = math.floor(budget / (risk + floor_cost))
    while contracts >= 1 and contracts * risk + order_costs(fills, contracts, tiers) > budget:
        contracts -= 1
    return contracts


def open_position(candidate, store, config):
    fills = tuple(_fill(leg.row, leg.side, opening=True, fill_ratio=config.fill_ratio)
                  for leg in candidate.legs)
    entry_cash = _opening_cash(candidate.legs, fills)
    return Position(candidate, fills, entry_cash,
                    find_exit(candidate, entry_cash, store, config))


def size_trades(positions, config):
    """Walk Positions in session order, skipping entries while
    MAX_OPEN_POSITIONS are open and sizing each on equity closed so far."""
    tiers = fee_tiers(config.underlying)
    trades, skipped, taken = [], [], []  # taken: (closed_ms, pnl)
    for position in sorted(positions, key=lambda p: p.candidate.session):
        candidate, closing = position.candidate, position.exit
        if sum(closed_ms > candidate.entry_ms for closed_ms, _ in taken) >= MAX_OPEN_POSITIONS:
            skipped.append(_skip(candidate.session, "position cap reached"))
            continue
        risk = risk_unit(candidate, position.entry_cash) * CONTRACT_MULTIPLIER
        if risk <= 0:
            skipped.append(_skip(candidate.session, "no risk: credit at or above the width"))
            continue
        equity = config.initial_equity + sum(
            pnl for closed_ms, pnl in taken
            if spread_bt._session_of(closed_ms) < candidate.session)
        contracts = contracts_within_budget(config.risk_pct * equity, risk,
                                            position.entry_fills, tiers)
        if contracts < 1:
            skipped.append(_skip(candidate.session, "risk of one position exceeds risk budget"))
            continue
        costs = (order_costs(position.entry_fills, contracts, tiers)
                 + order_costs(closing.fills, contracts, tiers))
        pnl = round(contracts * (position.entry_cash + closing.cash) * CONTRACT_MULTIPLIER
                    - costs, 2)
        taken.append((closing.closed_ms, pnl))
        trades.append({
            "session": candidate.session.isoformat(),
            "expiry": candidate.expiry.isoformat(),
            "legs": [{"right": leg.row.right, "strike": leg.row.strike,
                      "side": "long" if leg.side == LONG else "short",
                      "fill": round(fill, 6)}
                     for leg, fill in zip(candidate.legs, position.entry_fills)],
            "underlying_at_entry": candidate.spot,
            "entry_cash": round(position.entry_cash, 6),
            "risk_per_position": round(risk, 2),
            "costs": round(costs, 2),
            "equity_at_entry": round(equity, 2),
            "contracts": contracts,
            "settlement": candidate.settlement,
            "exit": closing.reason,
            "exit_time": spread_bt._et_iso(closing.closed_ms),
            "closed": spread_bt._session_of(closing.closed_ms).isoformat(),
            "exit_cash": round(closing.cash, 6),
            "pnl": pnl,
        })
    return trades, skipped


# ---------------------------------------------------------------------------
# Benchmark and the Holdout
# ---------------------------------------------------------------------------
def buy_and_hold(closes, first, last):
    """Price return of holding the underlying from the close of `first` to the
    close of `last`, or None with fewer than two closes."""
    days = sorted(d for d in closes if first <= d <= last)
    if len(days) < 2:
        return None
    returns = [closes[b] / closes[a] - 1 for a, b in zip(days, days[1:])]
    equity = peak = 1.0
    drawdown = 0.0
    for r in returns:
        equity *= 1 + r
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    sd = statistics.stdev(returns) if len(returns) > 1 else 0.0
    return {"first": days[0].isoformat(), "last": days[-1].isoformat(),
            "total_return": closes[days[-1]] / closes[days[0]] - 1,
            "sharpe": statistics.fmean(returns) / sd * math.sqrt(TRADING_DAYS) if sd else None,
            "max_drawdown": drawdown}


def window(start, end, holdout):
    """(first, last) session dates a run may replay: the Dev Period ends the day
    before HOLDOUT_START; a Holdout run starts on it."""
    if holdout:
        return max(start or HOLDOUT_START, HOLDOUT_START), end
    last = HOLDOUT_START - timedelta(days=1)
    return start, min(end, last) if end else last


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def run_configs(store_path, configs, start=None, end=None, holdout=False):
    """{config: report} for configurations on one underlying."""
    (underlying,) = {config.underlying for config in configs}
    first, last = window(start, end, holdout)
    store = Store(store_path, underlying)
    try:
        sessions = store.sessions(first, last)
        selections = sorted({config.selection for config in configs},
                            key=lambda s: (s[0], s[1], s[2] or 0))
        candidates = defaultdict(list)
        skipped = defaultdict(list)
        for session in sessions:
            chain, reason = store.entry_chain(session)
            for selection in selections:
                candidate, why = (None, reason) if chain is None else select_candidate(
                    session, chain, selection, store.closes)
                if candidate is None:
                    skipped[selection].append(_skip(session, why))
                else:
                    candidates[selection].append(candidate)
        reports = {}
        for config in configs:
            positions = [open_position(c, store, config) for c in candidates[config.selection]]
            trades, sizing_skips = size_trades(positions, config)
            reports[config] = {
                "config": asdict(config),
                "period": {"first": first and first.isoformat(),
                           "last": last and last.isoformat(),
                           "holdout": holdout},
                "sessions": len(sessions),
                "metrics": spread_bt.compute_metrics(trades, sessions, config.initial_equity),
                "exits": dict(Counter(t["exit"] for t in trades)),
                "trades": trades,
                "skipped": sorted(skipped[config.selection] + sizing_skips,
                                  key=lambda s: s["session"]),
            }
        benchmark = (buy_and_hold(store.closes, sessions[0], sessions[-1])
                     if sessions else None)
    finally:
        store.close()
    return reports, benchmark


def run_experiment(store_path, config, start=None, end=None, holdout=False):
    """One configuration's report, with buy-and-hold beside it."""
    reports, benchmark = run_configs(store_path, [config], start, end, holdout)
    return {**reports[config], "buy_and_hold": benchmark, "caveats": list(CAVEATS)}


def grid_configs(underlying, fill_ratio=0.5, initial_equity=OptionsConfig.initial_equity,
                 risk_pct=OptionsConfig.risk_pct):
    """Every strategy over its parameter grid, both exit rules."""
    configs = []
    for strategy in STRATEGIES:
        deltas = LONG_DELTAS if strategy == LONG_STRANGLE else CREDIT_DELTAS
        widths = ETF_WIDTHS if strategy in WINGED else (None,)
        if underlying == "SPX" and strategy in WINGED:
            widths = spread_bt.GRID_WIDTHS
        for delta in deltas:
            for width in widths:
                for rule in EXIT_RULES:
                    configs.append(OptionsConfig(
                        underlying=underlying, strategy=strategy, delta=delta, width=width,
                        fill_ratio=fill_ratio, exit_rule=rule,
                        initial_equity=initial_equity, risk_pct=risk_pct))
    return configs


def run_compare(store_path, underlyings, start=None, end=None, holdout=False,
                fill_ratio=0.5, initial_equity=OptionsConfig.initial_equity,
                risk_pct=OptionsConfig.risk_pct):
    """The comparison report: one row per configuration per underlying."""
    result = {"fill_ratio": fill_ratio, "holdout": holdout, "underlyings": {}}
    tried = 0
    for underlying in underlyings:
        configs = grid_configs(underlying, fill_ratio, initial_equity, risk_pct)
        tried += len(configs)
        reports, benchmark = run_configs(store_path, configs, start, end, holdout)
        result["underlyings"][underlying] = {
            "buy_and_hold": benchmark,
            "rows": [{**{k: report["config"][k] for k in
                         ("strategy", "delta", "width", "exit_rule")},
                      "period": report["period"], "sessions": report["sessions"],
                      "metrics": report["metrics"], "exits": report["exits"],
                      "skipped": len(report["skipped"])}
                     for report in reports.values()],
        }
    result["configurations_tried"] = tried
    result["multiple_testing"] = MULTIPLE_TESTING_LINE.format(tried)
    result["caveats"] = list(CAVEATS)
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _number(value, digits=2):
    return "n/a" if value is None else f"{value:.{digits}f}"


def _percent(value):
    return "n/a" if value is None else f"{value:.1%}"


def format_summary(compare):
    """The comparison as Markdown."""
    lines = [f"# Strategy comparison ({'Holdout' if compare['holdout'] else 'Dev Period'},"
             f" fill {compare['fill_ratio']:.0%} of the half-spread)", ""]
    for underlying, block in compare["underlyings"].items():
        lines.append(f"## {underlying}")
        lines.append("")
        bench = block["buy_and_hold"]
        if bench:
            lines.append(f"Buy-and-hold {bench['first']} to {bench['last']}:"
                         f" return {_percent(bench['total_return'])},"
                         f" Sharpe {_number(bench['sharpe'])},"
                         f" max drawdown {_percent(bench['max_drawdown'])}.")
            lines.append("")
        lines.append("| Strategy | Delta | Width | Exit | Trades | Sharpe | Sortino"
                     " | Max DD | CVaR 5% | Win | Total P&L |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for row in block["rows"]:
            m = row["metrics"]
            width = "" if row["width"] is None else f"{row['width']:g}"
            lines.append(
                f"| {row['strategy']} | {row['delta']:.2f} | {width} | {row['exit_rule']}"
                f" | {m['trades']} | {_number(m['sharpe'])} | {_number(m['sortino'])}"
                f" | {_percent(m['max_drawdown'])} | {_percent(m['cvar_5'])}"
                f" | {_percent(m['win_rate'])} | {m['total_pnl']:,.0f} |")
        lines.append("")
    lines.append(compare["multiple_testing"])
    lines.append("")
    lines.append("Caveats:")
    lines += [f"- {caveat}" for caveat in compare["caveats"]]
    return "\n".join(lines) + "\n"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Backtest option strategies on SPY/QQQ over the chain store.")
    parser.add_argument("--store", required=True, help="Path to the chain store SQLite file")
    parser.add_argument("--underlying", default="SPY",
                        help="SPY, QQQ (or SPX); with --compare, a comma-separated list")
    parser.add_argument("--strategy", choices=STRATEGIES, default=PUT_CREDIT_SPREAD)
    parser.add_argument("--delta", type=float, default=OptionsConfig.delta)
    parser.add_argument("--width", type=float,
                        help=f"Wing width in dollars (spreads only; default {DEFAULT_WIDTH:g})")
    parser.add_argument("--exit-rule", choices=EXIT_RULES, default=RULE_HOLD)
    parser.add_argument("--fill-ratio", type=float, default=OptionsConfig.fill_ratio)
    parser.add_argument("--equity", type=float, default=OptionsConfig.initial_equity)
    parser.add_argument("--risk-pct", type=float, default=OptionsConfig.risk_pct)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--holdout", action="store_true",
                        help=f"Replay only the Holdout (sessions from {HOLDOUT_START} on)")
    parser.add_argument("--compare", action="store_true",
                        help="Run every strategy over its grid instead of one configuration")
    parser.add_argument("--out", help="Also write the JSON report here")
    parser.add_argument("--summary", help="With --compare, write a Markdown summary here")
    args = parser.parse_args(argv)
    if args.summary and not args.compare:
        parser.error("--summary needs --compare")
    if args.compare and args.width is not None:
        parser.error("--compare runs its own widths; drop --width")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.compare:
        report = run_compare(args.store, args.underlying.split(","), args.start, args.end,
                             args.holdout, args.fill_ratio, args.equity, args.risk_pct)
        if args.summary:
            Path(args.summary).write_text(format_summary(report), encoding="utf-8")
    else:
        width = args.width if args.width is not None else (
            DEFAULT_WIDTH if args.strategy in WINGED else None)
        try:
            config = OptionsConfig(underlying=args.underlying, strategy=args.strategy,
                                   delta=args.delta, width=width, fill_ratio=args.fill_ratio,
                                   exit_rule=args.exit_rule, initial_equity=args.equity,
                                   risk_pct=args.risk_pct)
        except ValueError as exc:
            print(f"backtest_options: {exc}", file=sys.stderr)
            return 2
        report = run_experiment(args.store, config, args.start, args.end, args.holdout)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
