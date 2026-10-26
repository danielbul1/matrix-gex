# Quant Power: how the author defines his metrics (second public-source pass)

Research for "הגדרות משלנו לנוסחאות הסמויות" (#24, map #21). Follow-up to `docs/research/quant-power-catalog.md`. Question: for each Quant Power (QP) metric, **how does he do it**, using public sources only. We copy behaviour, not code, name, branding or data. Nothing behind the login was touched, and `/_dash-update-component` was never called.


> Local crops cited as `yt/...` and `img/...` were working files and are not committed (they are his screenshots). Each thumbnail is public at `https://i.ytimg.com/vi/<video id>/maxresdefault.jpg`.

## Headline

The first pass missed the author's **public YouTube channel, "Quant Power Trading" (@QuantPowerTrading, 29 videos)**. Every video description links `https://maxpowertrading.pythonanywhere.com`. The video **thumbnails are cropped screenshots of the signed-in dashboard**, from two UI generations, so they show how Net GEX, AG and Power Zone are drawn. Several also show an intraday price chart with a sideways Power Zone profile and horizontal level lines. No source states a formula in words. The videos' spoken content (transcripts) could not be retrieved.

Evidence tags:
- **seen**: visible in a source listed below.
- **inferred**: deduced from what is seen; the confidence is given each time.
- **guess**: our best guess if forced to choose. It is **not** evidence.
- **industry, not his**: a definition used by other GEX tools, given only as context.

## Sources (all accessed 2026-10-26, ~10:45–11:10 UTC)

| # | Source | URL | What it gave |
|---|---|---|---|
| S1 | Login page HTML (re-fetched) | https://maxpowertrading.pythonanywhere.com/ | Same as the first pass. `styles.css?m=1788815662` means the CSS file was last modified **2026-09-07 21:14 UTC**. Also has 3 clientside fullscreen functions and `adjustGEXLayout()`: the board and charts sit side by side, the charts column is **480 px** wide, and they stack below 1000 px. Inline scrollbar CSS for `#gex-options-board` |
| S2 | `/_dash-layout` | https://maxpowertrading.pythonanywhere.com/_dash-layout | The 8 card images are hosted on `i.postimg.cc`. Footer says "© 2020 Quant Power". No other content |
| S3 | `/_dash-dependencies` | https://maxpowertrading.pythonanywhere.com/_dash-dependencies | Unchanged: 24 callbacks. No Power Range and no modal |
| S4 | `/assets/styles.css` | https://maxpowertrading.pythonanywhere.com/assets/styles.css | Unchanged. It also contains an **unused image-modal block** ("МОДАЛЬНОЕ ОКНО ДЛЯ ИЗОБРАЖЕНИЯ"), `.feature-card:hover {cursor:pointer}` and a `.live-stats` 3-column grid. This suggests an earlier login page where clicking a card opened a screenshot, and which showed live stats. `#btn-power-range` is still styled but has no callback |
| S5 | `robots.txt`, `sitemap.xml`, `_dash-config` | same host | All three return the index HTML (catch-all route), so none of them exists |
| S6 | Card images (8) | `https://i.postimg.cc/{3JpcHcJC/stock-market, j2sNDCYh/pie-chart, Xvw5j9BP/1, tCNWG3Pm/magnet, 3xXCLMrV/classification, KcR9Pr4T/cctv-camera, Px0ppkHX/sand-clock, zGMNKwvj/transparency}.png` | Generic 512×512 cyan-blue gradient icons (magnet, hierarchy, chart, and so on). **No chart content.** Saved in `img/` |
| S7 | **YouTube channel "Quant Power Trading"** | https://www.youtube.com/@QuantPowerTrading (channel id `UCZLwsrZQCmfQ4sesdVepGxA`) | 29 videos, from about 1 year ago to 2026-09-30. Titles listed in the appendix |
| S8 | Channel RSS feed (the 15 latest descriptions) | https://www.youtube.com/feeds/videos.xml?channel_id=UCZLwsrZQCmfQ4sesdVepGxA | Every description has "👉Website: https://maxpowertrading.pythonanywhere.com", "Need help? DM our admin: https://telegram.me/AlabamaQuantAdmin", 📧 `tradingmaxpower@gmail.com` (2025-12/2026-01 videos), links to lessons #1–4, and chapter lists |
| S9 | oEmbed for 4 videos | `https://www.youtube.com/oembed?url=...` | Confirms the author is "Quant Power Trading" |
| S10 | **Video thumbnails (29)** | `https://i.ytimg.com/vi/<id>/maxresdefault.jpg` | **Screenshots of the dashboard charts.** Saved in `yt/thumb_*.jpg`, with contact sheets `yt/sheet_0..5.jpg` and zoomed crops `yt/z_*.png` |
| S11 | Telegram admin preview | https://t.me/AlabamaQuantAdmin | Name "Alabama", bio `tradingmaxpower@gmail.com`. This is the person who sells access |
| S12 | Telegram support invite (re-checked) | https://t.me/+ccPkiwklte01MDNi | "Quant Power support", 1,295 subscribers, no description. Private, so posts are not readable |
| S13 | `t.me/s/QuantPowerTrading` | https://t.me/s/QuantPowerTrading | **False lead.** An unrelated crypto "investment plan" spam channel (2 subscribers, contact @Accurate_Analysis1). It has no link to QP |
| S14 | Wayback Machine | `archive.org/wayback/available?url=…` for `/`, `/assets/styles.css`, `/_dash-layout`, `/how-to-use-gex`, `/key-levels`, `/gex-analysis`, `/_dash-update-component` | `archived_snapshots: {}` for all of them. The **CDX API** (`web.archive.org/cdx/...`) **could not be reached** from this sandbox (the connection was reset at the proxy, 3 tries). The first availability calls also got 429. So "no captures" is likely but not proven by CDX |
| S15 | archive.today | https://archive.ph/maxpowertrading.pythonanywhere.com | HTTP 429 (rate limit or captcha). Not bypassed. Unknown |
| S16 | GitHub code search (MCP) | ids `btn-power-zone`, `gex-options-board`, `btn-volume-spread`, `maxpowertrading`, `"power zone" "net gex"`, `"ABS GEX (AG)"` | **One repo with a QP id**: see "Report but do not extract" below. `maxpowertrading` only appears in an expired-domains list. `gex-options-board` and `btn-volume-spread`: 0 hits |
| S17 | Web searches (EN/RU) | `"maxpowertrading"`, `"Quant Power" GEX "Power Zone" опционы`, `"@QuantPowerTrading"`, `"AlabamaQuantAdmin" OR "tradingmaxpower"`, lesson titles, `"Power Zone" gamma … magnet`, Russian Power Zone / Volume Spread / магнит | Only the search for `"maxpowertrading"` surfaced the YouTube videos. Everything else was unrelated or generic. `quantpower.tech` (an Indian algo platform) and `quantpower.com` (energy) are **different companies** |
| S18 | Industry definitions | https://support.spotgamma.com/hc/en-us/articles/15297255426195-Absolute-Gamma ; TradingView "Options Volume" help; TradingView "Power Zones [BreakoutOrFakeout]" | Context only, see Q4 and Q5 |

### Report but do not extract (possible source code)

- **https://github.com/vlasenkoalexey/gex_dashboard**: public, Python, described as "Gamma Exposure (GEX) Dashboard — options GEX visualizer with multiple data providers". Created 2026-04-13, **last pushed 2026-09-07 22:02 UTC** (QP's `styles.css` was modified the same day at 21:14 UTC; this may be a coincidence). GitHub code search shows `app.py` contains the ids **`btn-power-zone`** and **`btn-net-gex`**, and `README.md`, `LLM.md` and `app.py` contain the phrase **"Power Zone"**. Searches for other QP-specific ids or strings in that repo (`selected-params`, `btn-power-range`, `forecast-text`, `gex-options-board`, "Quant Power", "pythonanywhere") return **0 hits**. So it is **probably not QP's own source**, but it may be a clone or reimplementation of QP's GEX page and is likely to contain a Power Zone formula. **I did not open or read any of its files.** You decide whether it is in bounds.
- **https://github.com/Gogi32211/sachoki**: `backend/gex_engine.py` and `frontend/src/components/CodeCandleChart.jsx` match `"power zone" "net gex"`. No QP ids or strings found ("Quant Power" or maxpowertrading: 0 hits), so its link to QP is weak. Not opened.
- No archived `_dash-update-component` payloads were found (the Wayback availability API returned nothing; CDX was unreachable).

---

## What the thumbnails show (the strongest new evidence)

The dashboard screenshots fall into two UI generations:

**Old UI (about 1 year ago and earlier; lessons #1–#4 and the early 2025 videos):** dark navy background. The legend reads **"ABS GEX (AG)" · "+ Gamma" · "− Gamma" · "Call OI" · "Put OI" · "Call Volume" · "Put Volume"** (`yt/thumb_WsKWDGZDT3Q.jpg`, `yt/thumb_lmvKwZ6NlmE.jpg`, `yt/thumb_GNpU7PbjE1A.jpg`). Left y axis "GEX". **Right y axis "OI - VOLUME - ABS GEX"**. X axis "STRIKE". Vertical orange line "Spot: $6000.19". Numbers use a space as the thousands separator ("5 920", "20 000"), which is a Russian or European locale. Watermark "**Max Power**" (`yt/z_uThg.png`), the older brand that matches the `maxpowertrading` username.

**Current UI (2025-H2 → 2026):** background `#1e1e1e`, watermark "Quant Power", ticker label at top left in **Yahoo-style symbols "^SPX" or "SPY"**, vertical orange line "**Price: 6595.56**", legend "Net GEX" (bars) and "AG" (line with markers). Net GEX is on the left axis and AG on a separate right axis.

---

## Q1. Power Zone

**(a) Seen**
- Login card (S2): "Unique development that identifies the range within which price will move."
- `btn-power-zone` (yellow `#e8ca01`) is one of 7 toggles feeding `selected-params` → `options-chart` (S3, S4).
- **Thumbnail `_aaHSyIOCKA` ("How to Turn $280 into $2,230 in 3 DAYS Using GEX", about 1 year ago, crop `yt/z_pz_chart.png`)**:
  - Legend: `[AG, greyed out]` `+Net GEX` (blue) `− Net GEX` (red) `[Call OI]` `[Put OI]` `[Call Volume]` `[Put Volume]` (all greyed out) **`POWER ZONE`** (yellow).
  - Power Zone is drawn as **a per-strike yellow spline with markers and a translucent fill to zero, on the right-hand secondary axis (0–120M)**, over the Net GEX bars (left axis, −1M to 7M).
  - Where the yellow fill overlaps, blue bars show light green and red bars show orange.
  - The curve **peaks at strike 6400, right at spot (Price: 6394.79)**, at about 128M, with secondary humps (about 70M at 6420, 40M at 6460).
  - The largest Net GEX bar (+6.7M at 6460) carries only a small Power Zone hump. So Power Zone is **not** proportional to |Net GEX|.
- **Thumbnail `dfEeVw03ahk`** ("How to Trade 0DTE SPX Using Gamma Exposure GEX", about 1 year ago, `yt/z_dfE.png`): the same yellow filled spline over blue and red Net GEX bars for SPX 5900–6045, peaking at 5960 next to the largest positive Net GEX bars.
- **Thumbnail `4Q_qDKgtspE`** ("How to Turn Small Deposit Into $2,190 in 1 DAY Using GEX", `yt/z_price_profile.png`):
  - An intraday SPX 1-minute candle chart for **Aug 5, 2025, 09:30–16:00**.
  - A **yellow profile drawn sideways along the price axis** on the left edge: Power Zone per strike, rotated, with its largest bulge at about 6300–6305.
  - Horizontal lines: **yellow at about 6300**, red at about 6295, cyan at about 6320, and a green/purple dashed line at about 6355.
  - Price is pinned to the yellow line for the whole afternoon (overlay text "THIS CHANGES EVERYTHING!").
- The Power Zone overlay appears only in videos from about a year ago. The 2026 thumbnails show only Net GEX + AG.

**(b) Inferred**
- Power Zone is a **per-strike series**, drawn as a filled area, **not a low/high band** (high confidence).
- The "range" in the marketing copy is presumably read off the hump: the strikes where the Power Zone mass sits around spot (medium).
- Its scale (about 10⁸ for SPX) is the same order as AG, so it is likely a gamma-exposure-type quantity, not a count (medium).
- Its shape is concentrated around spot rather than at the large-OI walls, so it probably weights by **proximity to spot and/or same-day volume** (medium-low).
- It is also projected onto the price chart as a sideways profile, with a line at its maximum (medium).

**(c) Guess** (labelled guess): Power Zone(K) = absolute gamma exposure per strike computed from **volume (or OI+volume)** rather than OI alone, so it bulges where today's trading concentrates near spot. The "zone" is the contiguous strikes around the maximum. If Matrix wants to match the *behaviour*:
- plot it as a filled per-strike area on its own axis;
- draw its peak as a horizontal level on the price chart, plus the sideways profile.

## Q2. "Quant Power" magnet level

**(a) Seen**
- Card text: "Unique development that identifies the magnet level that attracts price" (S2), with a magnet icon (S6).
- No dedicated component id (S3).
- In `4Q_qDKgtspE` (above), price is visibly "attracted" to and pinned at the **yellow horizontal line, which coincides with the peak of the yellow Power Zone profile**.
- In the 2026 thumbnails, the circled "target" strikes are always **at the AG spike**, where a large red Net GEX bar sits under the purple AG fill and shows as a magenta column:
  - "6590";
  - "6700 NOV 07'25" (`O6T2ghKILMA`);
  - "6720 JAN 09'26" (`o7_Ys5qBCMg`);
  - "655" (SPY, `Lu1_8M6Wt7w`);
  - "6000" (`_aePhp0naJ4`, Net GEX minimum);
  - "7000" (SPY 699/700 AG spike, `PLvfXEpmeCs`; crop `yt/z_PLv.png`).
- Text overlays: "Target", "Resistance", "Bear territory" (`KtcEHm8mn2o`, `CdiFDxMYOoI`).

**(b) Inferred**
- The magnet is a single price level drawn as a horizontal line on the price chart (`price-chart` / `key-levels-chart` / `gex-price-chart`) (medium).
- Two candidate definitions fit the pictures:
  - (i) the strike of maximum Power Zone, where the yellow line sits on the profile peak (medium);
  - (ii) the strike of maximum AG, which is what he circles as "target" in 2026 videos (medium).
- The yellow colour of the line matches the Power Zone colour, which favours (i) for the "Quant Power" level.

**(c) Guess**: Quant Power magnet = argmax over strikes of Power Zone, near spot, for the selected expiry. AG max is his informal "target".

## Q3. AVG Power Zone and Power Range

- **AVG Power Zone**: **not seen anywhere**: not in S2, S3, S4 or any of the 29 thumbnails. It appears to be Matrix's own invention.
- **Power Range**:
  - **seen**: only as a CSS rule, `#btn-power-range { background-color: #0090ea }` (S4), and still present in the current file.
  - Not in the callback graph (S3) and not in any thumbnail legend.
  - Not in archives (S14, S15), which were either empty or unreachable.
  - **inferred**: a removed or never-shipped toggle, perhaps an earlier "range" form of Power Zone (low confidence).

## Q4. AG

**(a) Seen**
- In the old UI the legend label is literally **"ABS GEX (AG)"**, shown with "+ Gamma" / "− Gamma" bars, on a right axis titled "OI - VOLUME - ABS GEX".
- In the current UI it is labelled "AG": a purple (`#915bf8`) spline with markers and a translucent fill, on a secondary right axis.
- AG spikes coincide with the strikes of the largest |Net GEX| (often large negative bars). For example:
  - SPY 740 (`M9ImYRMmZWg`);
  - SPX 6335 (`8CzB0eVLtQo`, crop `yt/z_8Cz.png`);
  - SPY 699/700.
- Scale: for SPX, Net GEX runs in the low millions (left axis) while AG runs to about 100–180M (right axis), and for SPY about 25M against Net GEX in the hundreds of thousands.

**(b) Inferred**
- AG = "absolute GEX" per strike. Its large values where Net GEX is modest imply it sums the magnitudes of call and put exposure, |call GEX| + |put GEX|, rather than netting them (high confidence on "absolute"; medium on the exact sum).
- The 10²-scale gap between the axes could also mean a different scaling constant from Net GEX (unknown).
- Old UI: AG shares an axis with OI and volume (count units), so the units may have changed between versions.

**(c) Guess**: AG(K) = Σ over calls and puts at strike K of |Γ · OI · 100 · S²(·0.01)|.
- Industry, not his: SpotGamma "Absolute Gamma" = |call gamma| + |put gamma|; the strike with the largest is treated as support/resistance and a "magnet" (S18).

## Q5. Volume Spread

- **(a) Seen**: only the button `btn-volume-spread` (first on the OI Volume page, coloured `#22b5ff`, the same blue as Net GEX), feeding `oi-volume-chart`. **No thumbnail shows it, and no public text explains it.**
- **(b) Inferred**: the "net" member of the OI/Volume set (low-medium), so most likely a signed per-strike difference between call and put volume, drawn as bars. The +Net GEX colour suggests positive-blue / negative-red, as with Net GEX.
- **(c) Guess**: Volume Spread(K) = Call Vol(K) − Put Vol(K) as signed bars (blue > 0, red < 0).
- Industry, not his: TradingView's Options Volume tool shows by-strike call/put volume but no "spread". "Volume delta" is the usual name elsewhere. **No tool found uses the exact term "Volume Spread" for options.**

## Q6. P/C Ratio table

- **Not public.** S3 shows `options-summary-table.data ← url` only: no ticker input, and it is computed on page load.
- **Inferred**: a fixed multi-ticker summary.
- The tickers seen across his videos are **^SPX** and **SPY** (also "QQQ"-style tickers are possible but not seen).
- The Yahoo-style `^SPX` label suggests a **yfinance-type data source** (medium), whose option chains give per-contract `volume` and `openInterest` per expiry. So both OI- and volume-based P/C ratios are possible.
- **Guess**: rows = tickers (SPX, SPY, QQQ, …), with columns for expiry, call OI, put OI, P/C OI, call vol, put vol and P/C vol.

## Q7. Forecast text

- **Seen**: the `forecast-text` children are rendered on the Key Levels page. CSS `#market-forecast` styles **h3** (gradient title), **h4** (white section heading) and **h5** (gradient sub-heading) (S4). It is regenerated on every ticker search, so it is generated by code, not written by hand (medium-high).
- No screenshot of the text exists publicly.
- His public narrative style (YouTube, English) is a strong guide:
  - chapters run "Example № 1…№ 8 — How GEX Predicts SPX Moves", then "Market Overview", "What to do now" and "About the market. Strategy for this and next week";
  - titles frame "where MMs will push SPX", "This Week's SPX Target", "Breakdown or Bounce?", "Bear territory / Target / Resistance".
- **Language**: the UI is English. The developer is Russian-speaking (CSS comments, "№", space thousands separators).
- **Guess**: an h3 title "Market forecast for <TICKER>", h4 sections such as "Current position" (spot vs Net GEX sign / zero cross), "Key levels" (resistance = largest +GEX, support/target = largest −GEX / AG, magnet), "Scenario" (bull/bear), and h5 conclusions. The text is rule-based on the same Net GEX / AG / Power Zone data.

## Q8. Validation and track record

- **Seen**: titles make P&L claims:
  - "+$3,000" and "+$3,600" (weekly model);
  - "How I Made 350%";
  - "$214 into $3,540 in next 2 weeks";
  - "$280 into $2,230 in 3 days";
  - "300% on SPX in 1 day", "600% … in 3 days", "1600% … next month" (thumbnails).
- Thumbnails mark a strike and a date (for example "6700 NOV 07'25", "6720 JAN 09'26"), which look like the option contract traded (strike plus expiry) (inferred, medium).
- Validation is **retrospective case studies** in videos ("Example № 1–8 How GEX Predicts SPX Moves").
- **No public statistics, hit rate, backtest or dated track-record log was found.** The access channel (Telegram support group) is private. The login page claims "© 2020", while the YouTube channel's visible history starts about 1 year ago. The CSS has an unused `.live-stats` block, perhaps for once-shown headline stats.
- Caution: profit-claim thumbnails are marketing, not evidence.

---

## Appendix: channel videos (newest first; views and age as shown 2026-10-26)

| id | Title | Views / age |
|---|---|---|
| Jo-weoytgUE | THE MARKET IS LYING TO YOU! … GEX Trading (2026-09-30) | 8.4K |
| mvYGUkDg9kc | HOW TO PROFIT FROM THE FED RATE DECISION … (2026-09-16) | 5.4K |
| Or2MWArTLu8 | 0DTE GAMMA TRADING MASTERCLASS … (2026-09-09) | 5.4K |
| KtcEHm8mn2o | The Worst Months Are Ahead … (2026-07-23) | 7.9K |
| t3vH5TbjRJo | This Always Happens Before a Crash \| How GEX Predicts SPX a Week Ahead (2026-07-09) | 6.5K |
| YBuiVUl5sy8 | GEX Weekly Model on SPX: Levels, Trades & Market Maker Strategy (+$3,000) (2026-06-23) | 3.6K |
| Z3Q9MyIHaiQ | The Storm Is Here … (2026-06-08) | 2.3K |
| M9ImYRMmZWg | A Storm is Brewing \| GEX & Gamma Squeeze (2026-06-03) | 4.5K |
| CdiFDxMYOoI | How to Combine Swing & Day Trading Using GEX (+$3,600) (2026-05-27) | 2.3K |
| KfD87txaW0c | How to Use GEX to Predict SPX Moves … (2026-05-15) | 5.5K |
| mtq2gsrs_GY | How GEX Predicts Trump's Pumps (2026-05-08) | 2.9K |
| PLvfXEpmeCs | MMs After FOMC: Where Will They Push SPX? \| GEX Forecast (2026-04-29) | 1.9K |
| dpUQCsA2QC4 | How GEX Predicts SPX a Week Ahead \| How I Made 350% (2026-04-22) | 4.3K |
| LlIyq-l5RAc | How to Trade 0DTE SPX Using GEX (2026-01-07) | 8.1K |
| o7_Ys5qBCMg | How GEX Predicts SPX Moves … The Ultimate 0DTE Tool (2025-12-29) | 4.1K |
| O6T2ghKILMA | How GEX Predicts ALL SPX Moves … | 25K / 11 mo |
| Lu1_8M6Wt7w | How GEX Predicts Trump's Actions … | 11K / 1 y |
| _aePhp0naJ4 | GEX is a Trader's Most Powerful Tool … | 17K / 1 y |
| U9GGZtVsWu8 | Gamma Exposure is the Most Powerful Tool … | 7.1K / 1 y |
| 7DMEBBC6dhk | THE MARKET IS DOING THE UNTHINKABLE … | 1.3K / 1 y |
| _o3l9vGzdx4 | WHAT WILL HAPPEN TO THE MARKET IN SEPTEMBER … | 1.1K / 1 y |
| 8CzB0eVLtQo | How to Turn $214 into $3,540 in next 2 WEEKS Using GEX | 3.2K / 1 y |
| 4Q_qDKgtspE | How to Turn Small Deposit Into $2,190 in 1 DAY Using GEX | 2.6K / 1 y |
| _aaHSyIOCKA | How to Turn $280 into $2,230 in 3 DAYS Using GEX | 1.1K / 1 y |
| dfEeVw03ahk | How to Trade 0DTE SPX Using Gamma Exposure GEX | 9.2K / 1 y |
| lmvKwZ6NlmE | The Only Sure Way to Draw Supply and Demand Zones … lesson #4 | 2.7K / 1 y |
| uThgJ_QMiNU | How to Find Supply & Demand Zones using Gamma Exposure \| lesson #3 | 3.2K / 1 y |
| GNpU7PbjE1A | THIS Will Improve your Day Trading … lesson #2 | 4.7K / 1 y |
| WsKWDGZDT3Q | How to use GEX to Determine Direction for Day Trading … lesson #1 | 23K / 1 y |

## Not done and why

- **Video transcripts** (lessons #1–4 would likely explain his reading of AG, Power Zone and the magnet): YouTube watch pages redirect to a bot check, and the public `timedtext` list returned empty. Getting around the bot check was refused by the sandbox's permission system, and I did not try other ways. A human watching lessons #1–#4 and `_aaHSyIOCKA` / `4Q_qDKgtspE` is the cheapest next step.
- **Wayback CDX and archive.today**: not reachable (proxy reset or 429), see S14 and S15.

## Addendum: `vlasenkoalexey/gex_dashboard` (opened 2026-10-26 at the user's request)

A public Dash app (https://github.com/vlasenkoalexey/gex_dashboard, last commit 2026-09-07) that mirrors Quant Power's strike-chart controls: the same button ids (`btn-net-gex`, `btn-ag`, … `btn-power-zone`) and the **same per-button colours as his `styles.css`** (`#915BF8`, `#E8CA01`, …). Its README does not mention Quant Power. It tells us nothing about his formula:

- **Power Zone** there is **the AG series redrawn** (`y=df["ag"]`, `app.py:1171-1177`) as a yellow smoothed spline. It is a placeholder, not a definition. It also peaks at the AG walls, unlike his screenshots, where the peak sits at spot.
- **AG** there is `call_gex − put_gex` with put GEX negated (`ib_data.py:192`), that is `|call GEX| + |put GEX|`. This is the same as Matrix and consistent with his "ABS GEX (AG)" legend.
