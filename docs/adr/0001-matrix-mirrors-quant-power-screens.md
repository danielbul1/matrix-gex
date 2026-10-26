# Matrix mirrors Quant Power's screens, and everything else is removed

Status: accepted (2026-10-26). Decided in "דרך העבודה במסחר עם Matrix" (#25, map #21).

The trader wants to work the way Quant Power's author works. No public source describes that workflow, so we define it as Quant Power's public screen structure: Strike Chart (home), OI Volume, GEX Board ("GEX Analysis"), Key Levels with its Forecast, P/C Ratio, How to use GEX and Disclaimer, in that menu order. Each screen has its own path under `/matrix/` and its own ticker input. Expiry choice copies his per-screen controls, and data reloads only on Refresh, never on a timer.

Every Matrix screen or control he does not have is deleted, not hidden. That covers DEX, VEX, CHEX, Market Structure, Exposure Lab, Net Drift & Flow, Matrix GEX, Shock Engine, Max Pain, Review, Journal, Replay and auto-refresh, plus the AVG Power Zone, Weighted, sigma-range and DEX/VEX/CHEX overlays. The Journal API, its `matrix_journal_entries` table and the stored entries are deleted too.

## Considered options

- **Keep the extra screens in an "Advanced" menu.** Rejected: the trader wants the site to look like his, not a superset of it.
- **Delete the Journal screens but keep the stored entries.** Rejected by the trader, despite the data loss being irreversible.
- **Make our own workflow (a morning call plus an evening grade).** Rejected: he does not have one, and the trader wants his.

## Consequences

The research side (Engine Label, the spread experiment) no longer has a place in the dashboard. The GEX Board table later gained a Notional Delta column, because the follower's copy of his app has one (#29). The DEX screen and overlay stay deleted. Copying stays behavioural only: we do not copy his code, name, branding or data, and we do not bypass his login (map #21, Notes).
