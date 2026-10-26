# Matrix against Max Power and gex_dashboard

Research for the second round of builds after map #21. The question is where Matrix, as deployed on 2026-10-26 (`186e09e`), differs from what is known about Max Power / Quant Power (the target), using the follower's public repos as a helper.

Sources:
- `vlasenkoalexey/gex_dashboard` (commits `ec23ce7`, `da08e2d`, `130487e`).
- `vlasenkoalexey/gex_weekly_strategy_validator`, including the first versions of `docs/power_zone_analysis.md` (`e8fb70f`, `814d9b7`).
- Earlier addenda in `quant-power-formulas.md`.

Both repos were read only. No code was copied.

## How much weight each source carries

- **Max Power, seen.** His public CSS, callback graph and page scripts, and his YouTube thumbnails. This is the target.
- **Max Power, as the follower reports it.** Values he read from the app: Power Zone per strike, the ±1% window, six levels with two-decimal values, about 6 expiries in the dropdown, one spot for all expiries, and a board that matched a "reference board" on strikes and OI. Strong, but second hand.
- **gex_dashboard's own design.** A helper only. It never names Max Power. Its "Power Zone" is just AG drawn as a spline, his own early guess that his later fit replaced.

## Gaps, most important first

| # | Where | Matrix today | Max Power (seen or reported) | gex_dashboard | Proposal |
|---|---|---|---|---|---|
| 1 | Key Levels: Zero Gamma | Where the cumulative Net GEX, summed from the lowest strike up, changes sign. On live data (2026-10-26 14:45 UTC) SPX, SPY and QQQ have **no Zero Gamma**, and NDX's sits 7.5% from spot | Not public | The crossing between two adjacent strikes whose Net GEX changes sign, nearest to spot (interpolated). The same data gives SPX 7,685.6, SPY 764.4, QQQ 712.2 and NDX 29,293.9, all next to spot | Switch to the nearest per-strike crossing. Ours fails on live data |
| 2 | Key Levels: Call Wall and Put Wall | Largest positive and most negative **Net GEX**. These are the same strikes as P1 and N1 | Not public. P1 and N1 already carry the Net GEX extremes | Largest **call GEX** and most negative **put GEX** | Decision needed. With gex_dashboard's definition the walls add something P1 and N1 do not. On today's SPX it puts both walls at 8,000 |
| 3 | Strike Chart: price label | "Price: 7,683", no decimals | Thumbnail: "Price: 6595.56", with two decimals | Shows the price with two decimals | Two decimals |
| 4 | Button and layer colours | Our palette (#23) | Seen in his CSS: Net GEX `#22b5ff`, AG `#915bf8`, Call OI `#02d432`, Put OI `#f32d35`, Call Vol `#003cfe`, Put Vol `#e55f04`, Power Zone `#e8ca01` | Same hexes | Decision needed. #23 kept our colours. Copying colours is behaviour, not branding, but it was decided before |
| 5 | Strike Chart: defaults | Net GEX and AG start on | 2026 thumbnails show Net GEX and AG | Only Net GEX starts on | Keep ours. The thumbnails are first-hand |
| 6 | Time to expiry | Intraday, with AM and PM settlement | Not public | Whole days, so 0DTE = 1/365 | Keep ours |
| 7 | GEX Board: % column | Net GEX over the total AG of **all** strikes | Not public | Net GEX over the AG of the **window** | Minor. Could match the helper |
| 8 | Expiry dropdown | 6 nearest future dates | About 6 dates, including one that had already expired on a weekend | Checklist with presets | Keep. Matches |

## Already matching

- Power Zone formula, ±1% window, drawing (spline 0.7, fill, right axis behind Net GEX), profile on the price chart (smoothing 0.8, width 2, $2.50 steps).
- Six level names with two-decimal values.
- AG formula, gross Notional Delta, the board's strike universe (OI > 0) and its 5% window.
- Click-only refresh.

## What neither repo shows

Nothing about Volume Spread, the P/C table, the Forecast text, the Key Levels page itself, the Gamma Total and Gamma Split charts, or the OI Volume page.

The author's own trading rules are in the follower's uncommitted files, not in the repo. What the repo does show is that he trades SPX and SPY spreads and condors with weekly expiries, from his video recaps. The video transcripts remain the best source for these.

## Answers from his own sources (2026-10-26)

We checked each open question against Max Power's own public material: his `styles.css` and all 29 YouTube thumbnails, which are cropped screenshots of the signed-in app. gex_dashboard was used only where he shows nothing.

1. **Zero Gamma: the per-strike sign change nearest spot.**
   - In `Or2MWArTLu8` he writes "Bear territory" and points it at the strike where the Net GEX bars turn from red to blue, next to the price line.
   - Across the other thumbnails that boundary always sits near the price line.
   - This matches gex_dashboard's definition. Matrix now uses it, and treats a strike with exactly zero Net GEX as lying on the crossing.
   - On live data it puts Zero Gamma at 7,685.6 (SPX), 764.4 (SPY), 712.2 (QQQ) and 29,293.9 (NDX), all next to spot. The old cumulative definition gave none for three of the four.
2. **Call Wall and Put Wall: unchanged (Net GEX).**
   - He reads levels off the Net GEX bars: "Resistance" on the large positive bars above price (`Or2MWArTLu8`), and "Target" on the largest negative bar.
   - Nothing of his uses call-only or put-only GEX.
3. **Price label: two decimals.** "Spot: $6000.19" (`WsKWDGZDT3Q`), "Price: 6408.40" (`8CzB0eVLtQo`), "Price: 764.29" (`mvYGUkDg9kc`).
4. **Colours: his.**
   - Sampled pixels match his CSS: positive Net GEX bars ≈ `#22b5ff`, negative bars pure red, AG `#915bf8` filled at about 45%, and Power Zone yellow at about 50%.
   - His buttons (CSS): Net GEX `#22b5ff`, AG `#915bf8`, Call OI `#02d432`, Put OI `#f32d35`, Call Vol `#003cfe`, Put Vol `#e55f04`, Power Zone `#e8ca01` with black text; 16px text, 10px padding, at least 70px wide; the active one scales to 0.92 with a soft inner glow.
   - This replaces the colour decision in #23, under the standing rule "like him".
