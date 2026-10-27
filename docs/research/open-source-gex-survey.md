# What open-source GEX projects can teach Matrix

Research for the session "פרויקטים מתקדמים של GEX בגיטהאב" (PR #67). The question is which advanced open-source GEX projects exist, what each does better or differently from Matrix, and whether their methods move our numbers when run on our own data.

Ground rules, decided with the trader:
- Ideas only. No code is copied into Matrix. Several of these repos are PolyForm Noncommercial, GPL, AGPL or unlicensed, which settles it anyway.
- An idea that would add a screen goes to its own section below, because ADR 0001 deletes every screen Quant Power does not have. Each one needs its own decision to reopen that ADR.
- `vlasenkoalexey/gex_dashboard` and `gex_weekly_strategy_validator` were covered in `gex-dashboard-comparison.md` and are left out here.

## Headline

1. **The gamma source flips SPX's sign.** On the same chain, Cboe's own gamma gives SPX a Naive GEX of **+11.1 $bn**; Black-Scholes on Cboe's IV gives **−21.1 $bn**. Cboe's greeks are computed at an underlying price other than the spot printed beside them.
   - Every serious project recomputes gamma from IV and uses the vendor's only as a fallback. Darthreign's `enrich` and puneet-chandna's calculator both do this.
   - Matrix mixes the two:
     - The research side (`tools/backtest_spread.py::naive_gex`) uses Black-Scholes only, while its docstring calls it "the dashboard's number".
     - The dashboard (`matrix.js::buildChainRealFromRecord`) prefers Cboe's gamma unless the record is tagged `lse_live_index` or `dynamic`. Live SPX and NDX are tagged; live SPY and QQQ are tagged `lse_live+cboe_oi`. So SPY and QQQ pair Cboe's gamma with the live LSE spot.
   - **Fixed in #68 (this PR):** the dashboard, `effective_greeks` and `build_exposure_history` now use Black-Scholes from IV at the spot shown. Cboe's greeks are used only for a row without IV. On this snapshot the dashboard now gives SPX −21.13, SPY −4.09 and QQQ −3.38.
2. **Per-strike Zero Gamma and the repriced Gamma Flip are different levels.** On this snapshot they sit 10 points apart on SPX, 2.8 on SPY and 8.5 on QQQ (1.2% of spot).
   - Both are now glossary terms. Zero Gamma stays the Key Level, as Quant Power reads it.
3. **How finely the flip is searched barely matters here.** We tried our 201-point linear grid, Darthreign's ±8% 161-point grid, gexkit's threshold plus bisection, and puneet-chandna's exact root-finding. They agree to within 0.03 points, because every scope had a single crossing. Both projects run unmodified reproduce our profile at spot exactly once given our inputs.
   - Our existing `matrix_gex.zero_gamma_flip` still takes the **first crossing from the bottom** of the grid, the bug gammagrid documents (161 of 200 snapshots had several crossings). It is wrong whenever there are several. Only `server.py` calls it today.
   - gflows has the same rule, and on the 0DTE scope it put SPX's flip at 4,104, against a true crossing near 7,667.
4. **Inputs matter more than method.** On SPX's total:
   - r and q move it by 10 $bn: our 5% and 0% against 3.63% and 1.5%.
   - Volume-for-OI on short expiries and whole-day time to expiry move it by 20 $bn, as in gflows.
5. **The near expiries decide today's flip.** With everything through the 11-20 OPEX removed, SPX's flip drops 141 points, and spot moves into positive gamma. That is the regime a multi-day spread meets after expiry.
6. **The research null to beat already exists.** marcusdrewry/gex-forward-returns finds that GEX's return effect disappears once VIX and realised vol are controlled for. Our IV-Matched Control is built to test exactly that.
   - federrico12's thesis adds what the spread experiment lacks: every result rerun under each Sign Convention.

## The snapshot used

- Cboe delayed chains fetched with our own `fetch_cboe.py` on 2026-10-27 at 13:09 UTC: the close of 2026-10-26.
  - SPX 7,676.77, 18,531 options.
  - SPY 768.23, 6,893.
  - QQQ 716.10, 5,785.
- Valued at 2026-10-27 10:00 ET, the spread experiment's entry time. Our own `years_to_expiry` was used: SPX monthlies settle at 9:30 ET, everything else at 16:00 ET.
- r = 0.05, q = 0, OI ≥ 10 unless stated. GEX = γ·OI·100·S²·1%, calls + and puts −.
- The published `data` branch was last updated 2026-07-16, so it was not used. This needs its own check: the README says pipeline A publishes there every 30 minutes.

## Numbers

Units are $bn per 1% move. "Cboe γ" is the dashboard's rule: Cboe's gamma when present, otherwise Black-Scholes. "BS γ" is Black-Scholes from IV for every row.

### All expiries

| | SPX | SPY | QQQ |
|---|---|---|---|
| Naive GEX, Cboe γ | **+11.10** | −5.04 | −3.87 |
| Naive GEX, BS γ | **−21.13** | −4.09 | −3.37 |
| Zero Gamma (per strike, nearest spot), Cboe γ | 7,703.67 | 767.61 | 715.53 |
| Zero Gamma, BS γ | 7,703.96 | 767.61 | 715.45 |
| Gamma Flip, Matrix 201-pt ±10% | 7,693.97 | 770.41 | 723.94 |
| Gamma Flip, Darthreign-style 161-pt ±8% | 7,693.97 | 770.41 | 723.94 |
| Gamma Flip, gexkit-style bisection | 7,693.99 | 770.41 | 723.94 |
| Crossings within ±10% | 1 | 1 | 1 |
| Call Wall / Put Wall (Net GEX) | 8,100 / 7,600 | 800 / 750 | 760 / 700 |
| Darthreign-style side-aware walls | 8,100 / 7,600 | 800 / 750 | 760 / 700 |

### Nearest expiry only (2026-10-27)

| | SPX | SPY | QQQ |
|---|---|---|---|
| Naive GEX, Cboe γ | +4.26 | +0.20 | +0.67 |
| Naive GEX, BS γ | −10.06 | +0.28 | +0.72 |
| Zero Gamma, Cboe γ | 7,698.60 | 767.67 | 712.85 |
| Gamma Flip (all three methods) | 7,697.86–7,697.87 | 767.90 | 712.54 |
| Call Wall / Put Wall | 7,720 / 7,650 | 770 / 767 | 720 / 712 |

### Where the SPX gap comes from

The vendor-minus-BS gap of +22 $bn is not spread evenly. By side and moneyness:
- ATM calls: +9.3.
- Puts 1–5% below spot: +12.5.
- ITM calls: +6.8.
- ATM and ITM puts: −3.7 and −3.4.

That pattern is what gamma computed at a higher underlying would give. On the 2026-10-27 expiry the 7,680 call shows Cboe Δ 0.76 against BS Δ 0.49 at the printed spot.

The later expiries agree much better (2026-11-20 ATM gammas within 5%), so the problem is concentrated in the nearest expiries, where gamma is most sensitive to spot.

## 0DTE-dealer-gamma and gflows on our data

Both were run unmodified, in their own virtualenvs, with every network and app import stubbed out. Each one, fed our inputs (OI, OI ≥ 10, our T, r = 0.05), **reproduces our profile at spot exactly and our Gamma Flip to within 0.1**. That validates `matrix_gex.gamma_profile`. The gaps come from their defaults.

### puneet-chandna/0DTE-dealer-gamma

| All expiries | SPX | SPY | QQQ |
|---|---|---|---|
| Their GEX with our inputs | −21.12 | −4.09 | −3.36 |
| Their zero gamma with our inputs | 7,693.99 | 770.41 | 723.94 |
| Their GEX with their defaults (r 3.63%, q 1.5%, ±20% strikes) | −30.33 | −4.98 | −3.80 |
| Their zero gamma with their defaults | 7,701.39 | 770.94 | 725.38 |

- **Rate and dividend yield alone move SPX's total by 10 $bn**: −20.16 to −30.40, changing one setting at a time. Our r = 0.05 and q = 0 are fixed constants. FRED's bills on 2026-10-23 were 4.11% at 1 month and 4.22% at 3 months.
- Their charm (+30 minutes) and vanna (−1 vol point) hedge flows, in notional $bn, positive meaning dealers buy:
  - Charm: SPX +0.97, SPY +0.15, QQQ +0.08.
  - Vanna: SPX +58.3, SPY +6.0, QQQ +3.2.
- Matrix has no equivalent for these.
- Their grid is 257 log-spaced points over ±20%, plus every strike and each contract's ±1σ flanks, with each bracket then solved exactly.
  - Our linear interpolation is off by at most 0.016 points.
  - Flank sampling only matters when a gamma spike is narrower than the grid step. Here the 0DTE ±1σ is 31–36 SPX points, against our 7.7-point step.

### aaguiar10/gflows

| All expiries, their method as written | SPX | SPY | QQQ |
|---|---|---|---|
| Bar total (Cboe γ, volume for ≤ 1 DTE) | +35.98 | +2.56 | −2.31 |
| Profile at spot | −1.77 | −1.21 | −3.36 |
| Gamma flip | 7,678.5 | 769.10 | 724.07 |
| Delta flip | 7,514.9 | 765.01 | 717.85 |

- **Volume instead of OI for anything ≤ 1 day out.** SPY's 10-27 volume is 1.07M against 367k OI, which by itself flips SPY's sign and pulls the walls onto spot. With OI everywhere, every wall matches ours.
- **Whole business days, floored at 1/252**, so 0DTE and 1DTE both get a full day. That is most of why their SPX profile at spot reads −1.8 against our −21.1.
- **Their flip takes the first crossing from the bottom**, the same as our `zero_gamma_flip`. That is harmless with one crossing. On the 0DTE scope it gives SPX 4,104 and SPY 627.8, against true crossings near 7,667 and 763.
- **Removing expiries, the lesson for spreads held for days.** Their `ex_next` / `ex_fri` profiles hold only those expiries, despite the names, so the "removed" profiles below are the total minus those.

| Gamma Flip | All | Next expiry removed | Through 11-20 OPEX removed |
|---|---|---|---|
| SPX | 7,678.5 | 7,682.5 | **7,537.1 (−141, −1.8%)** |
| SPY | 769.1 | 772.0 | 761.1 |
| QQQ | 724.1 | 728.6 | 730.0 |

With our inputs SPX moves 7,693.9 → 7,691.4 → 7,545.4. Once everything through OPEX is gone, SPX's profile at spot turns from negative to **+22.7 $bn**. The structural regime beneath today's is the opposite one, sitting 2% away.

## The projects

Quality is judged from the code, not the stars; most of the field is from 2026 and has few stars. "Serious" means its methods hold up when read closely.

| Repo | Lang · licence | Quality | What to learn |
|---|---|---|---|
| [federrico12/dealer-hedging-thesis](https://github.com/federrico12/dealer-hedging-thesis) | Python · none | serious | Every result rerun under three Sign Conventions; flip on a ±10%, 201-point repriced grid that reports the number of crossings and NaN when there is none; HAC t-stats, non-overlapping windows, ATM-IV control, permutation test around monthly OPEX. The template for the spread experiment. |
| [puneet-chandna/0DTE-dealer-gamma](https://github.com/puneet-chandna/0DTE-dealer-gamma) | Py + Next.js · PolyForm NC | serious | Flip root-finding with contracts merged on (K, IV, T); log-space gamma rescaled by its max, so tail underflow cannot fake a zero; a 257-point geomspace grid plus spot, strikes and ±1σ flanks. Charm and vanna as finite-difference scenarios (+N minutes, −1 vol point) expressed as hedge flow. |
| [mervinj3546/gexkit](https://github.com/mervinj3546/gexkit) | Rust · MIT/Apache | serious, early | Grid sweep, then bisection to 1e-10·spot; values below 1e-9 of peak count as zero; returns every crossing; a neutral band for the regime. Tested against a 4001-point Python oracle. A good model for our own flip tests. |
| [aaguiar10/gflows](https://github.com/aaguiar10/gflows) | Python Dash · MIT | decent→serious | DEX, GEX, vanna and charm over a 300-level grid (numba); tenor-matched Treasury rate; profiles **without the next expiry** and **without everything through the next monthly OPEX**; a delta flip next to the gamma flip. |
| [Darthreign/gex-dashboard](https://github.com/Darthreign/gex-dashboard) | Python Dash · MIT | serious, closest to us (Cboe delayed) | Recomputes every gamma from IV. Walls are ranked at a **frozen reference spot** (the previous close), so they do not drift intraday. Call wall searched only above spot, put support only below. OI-weighted flip and volume-weighted "HVL". Vanna flip. Backtest rules: levels taken at the session start only, an untouched level does not count as held, and a break needs a margin (median overshoot 0.24% on SPX). |
| [simonro/Edge-GEX-Lab](https://github.com/simonro/Edge-GEX-Lab) | FastAPI + React · MIT | serious engineering | AM/PM settlement with exchange early closes; coverage and OI-freshness gates; IV rebuilt from OTM pairs; neutral regime when \|net\| < k·gross; GEX as a share of ADV and per 1-ATR move; a percentile against frozen 10:00 captures (we already enter at 10:00); refuses to scale SPX by SPY volume. |
| [zrack/gex-terminal](https://github.com/zrack/gex-terminal) | Python TUI · MIT | deep, ES/NQ | "Wind tunnel": reprice under spot, IV and time shifts, with the nearest expiry removed and a 54-case grid; aggressor-signed volume, with unknown-aggressor volume kept out; expiry timestamps take precedence over DTE; IV-age provenance. |
| [emlama/gex-backtesting](https://github.com/emlama/gex-backtesting) | Python · none | decent research | SPX 0DTE trades with quote-matched sides; "side-weighted GEX"; concentration metrics GCI (Herfindahl of GEX by strike) and PGR (share within ±$20); pre-registered hypotheses with Benjamini-Hochberg. Its sign comments are inconsistent. |
| [EazyDuz1t/EzOptions-Schwab](https://github.com/EazyDuz1t/EzOptions-Schwab) | Flask · GPL-3 | decent | A price×time exposure surface repriced to the close, with the "now" column stored every minute, so history shows what was displayed then rather than a later repricing. |
| [gammagrid/gammagrid](https://github.com/gammagrid/gammagrid) | Streamlit · AGPL | cautionary | Its docstring documents the first-crossing bug: in 161 of 200 snapshots the cumulative-sum flip had several crossings, and taking the first one put SPX's flip at 2,800 with spot at 7,711. |
| [FullStackCraft/floe](https://github.com/FullStackCraft/floe) | TypeScript · MIT, non-commercial | ideas, some bad math | Live OI estimate (open interest plus intraday aggressor trades); total-variance IV smoothing. It uses the opposite dealer sign and adds GEX, VEX and CEX into one number, which mixes units. |
| [marcusdrewry/gex-forward-returns](https://github.com/marcusdrewry/gex-forward-returns) | Python | research | SqueezeMetrics GEX 2011–2026, past-only z-score, Newey-West, VIX/RV/momentum controls: no return effect once vol is controlled for. |
| [DarkHorseTrading/dealer-gamma-precision](https://github.com/DarkHorseTrading/dealer-gamma-precision) | CSV · CC BY 4.0 | research | Levels behave as zones of about $2–5, and the flip is the least precise level. 0DTE walls drift between 10:15 and 15:00. Retention scored with a tolerance instead of exact strikes. |
| [itsfabtrading/Gex-Multi](https://github.com/itsfabtrading/Gex-Multi) | Python · Apache | decent | Every parameter is a named constant logged with each run. Separate 0DTE flip and walls, and the 0DTE share of GEX. |

Rejected:
- **Matteo-Ferrara/gex-tracker**: a 160-line toy with no flip.
- **EzOptions**: the old yfinance version of EzOptions-Schwab.
- **iAmGiG/gex-llm-patterns**: about LLM evaluation, not GEX.
- **moremeds/argon**: cumulative-strike flip, depends on paid data.
- **Kza56/OFK_Atas_GEX** and **YichengYang-Ethan/0dte-strategy**: no reusable engine.
- **jensolson/SPX-Gamma-Exposure**: the classic 2021 notebook. It is only useful for its OCC open-interest cross-check and its filters (OI > 10, bid > 0.05, |Δ| < 0.95).

## Ideas that improve what exists

Opened on 2026-10-27, with the trader's go-ahead:
- 1 → #68
- 2 → #69
- 3 → #70
- 6 → #71
- 7 → #72

Ideas 4, 5 and 8 wait. The screen ideas below stay closed, and ADR 0001 is not reopened for now.

1. **One gamma source everywhere: Black-Scholes from IV at the spot shown.** Cboe's gamma is used only when IV is missing.
   - Fixes the SPY and QQQ live mismatch and the SPX sign flip.
   - Makes the research side's "the dashboard's number" true.
2. **Fix `zero_gamma_flip` to take the crossing nearest spot, and return every crossing.** Use a significance threshold, and treat "none" as a valid answer.
   - Add a flip test against a dense-grid oracle, the way gexkit does.
3. **Sign Convention robustness in the spread experiment.** Rerun the GEX Filter under all three conventions; a filter that only works under one is a warning.
4. **Neutral band on Gamma Regime.** Neutral when |net| < k·gross, as in Edge-GEX-Lab and gexkit. The dashboard's `calcGEX` already uses 4% of gross for its regime, but the glossary's Gamma Regime has only two signs. Decide whether the research uses the same band.
5. **Walls at a frozen reference spot.** Rank Call Wall and Put Wall with gamma at the previous close, since OI changes only daily, so they do not wander intraday (Darthreign).
6. **Data-quality gates.**
   - Coverage and OI-freshness checks before a snapshot is used.
   - Exchange early closes in `years_to_expiry`.
   - Find out why the `data` branch stopped on 2026-07-16.
7. **Rate and dividend yield from data, not constants.**
   - Use a tenor-matched bill rate (gflows) and a dividend yield per underlying.
   - On SPX they move Naive GEX by up to 10 $bn on this snapshot.
8. **Backtest rules for any level study.** Levels taken from the session start only; an untouched level is not "held"; a break needs a margin (Darthreign). Pre-registered hypotheses with FDR correction (emlama).

## Ideas that would add a screen (each needs ADR 0001 reopened)

- Profiles and flip **without the next expiry** and **through the next monthly OPEX** (gflows). This is what a multi-day spread lives through.
- Vanna flip and delta flip (gflows), and charm and vanna hedge flow as "+N minutes" and "−1 vol point" scenarios (puneet-chandna, Darthreign).
- Volume-weighted flip ("HVL") beside the OI flip (Darthreign).
- A price×time exposure surface, with the displayed value stored each minute (EzOptions-Schwab), and an intraday level-drift chart (DarkHorseTrading).
- Levels drawn as zones of about $2–5 rather than lines (DarkHorseTrading).
- OI-change tracking day over day (gammagrid, Darthreign `oi_change`).
- Concentration metrics GCI and PGR (emlama).
- Dealer side from aggressor-signed trades (zrack, floe, emlama). This needs trade-level data, which Cboe's delayed snapshot does not have.

## Reproducing

The scripts live outside the repo, in the session scratchpad, and are not committed:
- `build_baseline.py`: normalises the snapshot and computes Matrix's numbers with `matrix_gex`.
- `methods_reimpl.py`: our own implementations of the Darthreign and gexkit flip and wall methods, written from reading their code.
- `zg_bs.py`: the per-strike levels with Black-Scholes gamma.

How each project was checked:
- **Reimplemented:** running Darthreign's and gexkit's code directly was blocked by the session's sandbox (external code), so both were reimplemented from their code.
- **Run unmodified:** 0DTE-dealer-gamma and gflows ran in isolated virtualenvs, with network imports stubbed. Their adapters and result files (`results_odte.json`, `results_gflows.json`) are in the same scratchpad.
