# matrix-gex

A dealer-exposure dashboard for index options, and the research that tests whether its signals are worth trading on.

## Exposure

**GEX**:
Estimated dollar gamma that dealers hold across the whole SPX/SPXW chain, built from start-of-day open interest.
_Avoid_: gamma, gamma exposure level

**Naive GEX**:
GEX computed under the assumption that dealers are long every call and short every put.
_Avoid_: dealer gamma, true GEX

**Gamma Regime**:
The sign of Naive GEX for a session: positive gamma or negative gamma.
_Avoid_: GEX regime, gamma state

**GEX Percentile**:
Where today's Naive GEX sits within its own trailing history, using only past sessions.
_Avoid_: GEX rank, normalized GEX

**SOD Open Interest**:
Open interest known at the open of session T, reflecting positions at the close of T−1.
_Avoid_: today's OI, EOD OI

**Engine Label**:
The regime engine's classification of a session: GRIND_UP, TRAP_DOOR, SQUEEZE, PIN or MIXED.
_Avoid_: regime, signal

## Spread experiment

**Baseline**:
The unfiltered strategy: the put credit spread the experiment enters every session, with no filter applied.
_Avoid_: control, benchmark

**GEX Filter**:
The Baseline restricted to sessions selected by Gamma Regime or GEX Percentile.
_Avoid_: gamma filter, treatment

**Engine Filter**:
The Baseline restricted to sessions selected by Engine Label.

**IV-Matched Control**:
The Baseline restricted by a VIX threshold chosen to trade the same number of sessions as the filter it is compared against.
_Avoid_: VIX filter, control

**Rejected Sessions**:
The sessions a filter declines to trade, reported alongside the sessions it keeps.

**Undecided Session**:
A session a filter has no input for: its Naive GEX is missing, its GEX Percentile has too little past history yet, or no VIX close precedes it. It sits outside that filter's comparison, neither kept, rejected, nor counted in the Baseline beside them.
_Avoid_: warm-up, unfiltered

**Skipped Session**:
A session the backtester could not trade: data was missing, the risk budget could not fit one spread, or the Position Cap was reached. Unlike a Rejected Session, no filter chose to pass on it.
_Avoid_: rejected, filtered out

**Position Cap**:
The most spreads the experiment holds open at once. A spread stays open until it is closed or settles.
_Avoid_: max trades, slot limit

**Exit Rule**:
How an open spread is closed: held to settlement, or managed by a take-profit and a stop on the spread's buy-back cost.
_Avoid_: management style

**Fill Level**:
How far from mid an order fills, as a share of the half-spread: mid, 50%, or the full spread.
_Avoid_: slippage

**Headline**:
The grid's results at the 50% Fill Level: one row per short delta, width and Exit Rule.
_Avoid_: main results, summary

**Fill Sensitivity**:
The grid rerun at every Fill Level, showing how much a result depends on execution.
_Avoid_: slippage test

**Dev Period**:
The span of history used to choose parameters.
_Avoid_: in-sample, training period

**Holdout**:
The span of history held back from parameter choice and run once, after the Dev Period is frozen.
_Avoid_: test set, out-of-sample, OOS

## Dashboard

**Strike Chart**:
The home screen: one ticker and one expiry, with Net GEX, AG, OI, volume and Power Zone drawn per strike beside the price.
_Avoid_: GEX view, main chart

**OI Volume**:
The screen that shows one ticker and one expiry with Volume Spread, call and put OI and call and put volume per strike, and an intraday price chart with a line at the strike of the most call OI, put OI, call volume and put volume.
_Avoid_: OI page, volume view

**GEX Board**:
The screen for one ticker over a set of selected expiries: a strike table beside a Gamma Total, a Gamma Split and a price chart. Its menu label is "GEX Analysis".
- The table has one row per strike, summed over the selected expiries. Its columns are Strike, Net GEX, AG, Notional Delta, Total OI, Call OI, Put OI, Total Vol, Call Vol, Put Vol and %, where % is the strike's Net GEX as a share of the total AG.
- It covers strikes within 5% of spot, the same window as the Gamma Split. It runs from the highest strike down, scrolls, and sorts by any column.
- It highlights the spot row, the rows of the Key Levels and of P1, N1 and AG Strike, and each column's largest value.
- A line above it gives spot, Total Gamma (Net GEX summed over the selected expiries), its sign as the regime, the number of strikes and the ticker.
- The price chart carries the Price Lines, computed over the selected expiries.

_Avoid_: GEX Analysis page, heat map

**Gamma Total**:
The GEX Board chart of Net GEX summed per expiry: one bar for each of the ticker's expiries, whichever expiries are selected.
_Avoid_: total GEX, gamma by strike

**Gamma Split**:
The GEX Board chart of each strike's call GEX and put GEX, over the selected expiries and strikes within 5% of spot, drawn on opposite sides of zero.
_Avoid_: AG, split by expiry

**Notional Delta**:
A strike's delta in dollars with calls and puts both counted as positive: the delta of each contract, taken as a magnitude, times its open interest, 100 and spot, summed over the selected expiries. It is a GEX Board column only.
_Avoid_: DEX, net delta, delta exposure

**Key Level**:
One of the four prices the Key Levels screen marks from a ticker's GEX profile: Zero Gamma, Max Gamma, Call Wall and Put Wall. Max Pain is not a Key Level.
_Avoid_: support/resistance, levels

**AG**:
A strike's GEX with calls and puts both counted as positive, so the two sides add instead of cancelling.
_Avoid_: absolute gamma, ABS GEX, aggregate gamma

**Power Zone**:
A per-strike measure of where dealer hedging is heaviest. It is a strike's AG plus a share of its trading volume so far today, drawn only for strikes near spot.
_Avoid_: power range, AVG Power Zone, zone band

**Matrix** (the level):
The strike with the highest Power Zone: the level that price is drawn to. It is a Price Line, not a Key Level. It carries the product's own name, the way Quant Power names its magnet after itself.
_Avoid_: Matrix Power, Max Power, magnet, Quant Power level

**Price Line**:
One of the six strikes drawn across the price charts of the Strike Chart and the GEX Board:
- P1: the largest positive Net GEX.
- N1: the most negative Net GEX.
- Call Vol: the most call volume.
- Put Vol: the most put volume.
- Matrix.
- AG Strike: the largest AG.

_Avoid_: Key Level, support/resistance

**Volume Spread**:
A strike's call volume minus its put volume, for the session so far.
_Avoid_: net volume, volume imbalance

**P/C Ratio**:
Put over call for one ticker across all its expiries, measured both by open interest and by volume.
_Avoid_: PCR, put/call skew

**Forecast**:
The sectioned text read of one ticker, shown under the Key Levels chart and loaded with it.
_Avoid_: Market Read, Scenario Ladder, outlook

**Refresh**:
Reloading a screen's data because the trader asked (Search, Enter, or an expiry or ticker button). The dashboard never reloads on a timer.
_Avoid_: monitoring, live, auto-refresh, polling
