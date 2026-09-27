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
