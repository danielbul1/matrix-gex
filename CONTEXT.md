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
The unfiltered strategy: a 7DTE SPXW put credit spread entered every session at 10:00 ET.
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

**Dev Period**:
2018–2023, the only span used to choose parameters.
_Avoid_: in-sample, training period

**Holdout**:
2024–2026, run once after the Dev Period is frozen.
_Avoid_: test set, out-of-sample, OOS
