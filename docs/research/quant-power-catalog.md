# Quant Power: public catalog of screens, metrics and controls

Research for issue #22 (map #21). Question: what can be learned about Quant Power's screens, metrics and controls **from public sources only**, and which of them Matrix already has?

Rule for this note: we copy *behaviour* (which screens, metrics and controls exist and how they are used). We do not copy Quant Power's code, name, branding or data. Their callbacks were not called and their login was not bypassed.

## Summary

- Quant Power is a Plotly **Dash 2.18.1** app (dash_table 5.2.12, dcc 2.14.2) hosted on PythonAnywhere. All of its logic runs on the server. The only public surface is the login shell, the callback graph and one stylesheet.
- The signed-in app is a **sidebar plus 6 routes**: `/` (brand link), `/oi-volume`, `/gex-analysis`, `/key-levels`, `/options-summary` (labelled "P/C Ratio"), `/how-to-use-gex` and `/disclaimer`. A `url` Location component feeds `page-content`, and `url.search` is an input too, so query strings matter on some page.
- The callback graph has **24 callbacks**. They cover four interactive work areas: a GEX/parameter chart page, an OI/Volume page, a Key Levels page with forecast text, and a "GEX board" (a strike table with expiry and ticker toggle buttons plus three expandable charts). There is also a P/C summary table that loads with the page.
- **No `dcc.Interval` drives any callback.** Every update fires on a click, an Enter key or a URL change. Nothing in the callback graph shows the "Monitoring: real-time tracking of changes" feature from the marketing text.
- Public web search found **nothing** beyond the site itself: no videos, posts, forum threads or TradingView ideas. The only off-site trace is the Telegram invite from the login page, "Quant Power support" (1,295 subscribers), and its preview has no description. The CSS comments are in Russian, which suggests a Russian-speaking author and audience. No public source explains any formula.

## Sources

All accessed **2026-10-26** (09:31 UTC). Six GETs went to the site in total: the page, the layout, the dependencies and one CSS file, plus the Telegram preview.

| # | Source | URL | What it gave |
|---|---|---|---|
| S1 | Login page HTML | https://maxpowertrading.pythonanywhere.com/ | Title "Quant Power", Dash and component versions, one stylesheet |
| S2 | Dash layout | https://maxpowertrading.pythonanywhere.com/_dash-layout | Login panel, 8 marketing feature cards, the sidebar with route paths, `url` and `page-content` |
| S3 | Dash callback graph | https://maxpowertrading.pythonanywhere.com/_dash-dependencies | 24 callbacks: component ids, inputs and states (the controls) |
| S4 | Stylesheet | https://maxpowertrading.pythonanywhere.com/assets/styles.css | Per-button colours, page CSS classes, `#market-forecast` heading structure, a **`#btn-power-range`** that is not in S3 |
| S5 | Telegram invite linked from the login page ("Get access") | https://t.me/+ccPkiwklte01MDNi | Channel "Quant Power support", 1,295 subscribers, no description |
| S6 | Web searches (English and Russian) for "Quant Power" GEX / Power Zone, "maxpowertrading", "MaxPower Trading", "Quant Power support" telegram, and YouTube | (search engine) | Nothing about this product. Only an index entry for S1. Hits were unrelated: QuantWheel, Quantower, TradingView "Power Zones" scripts |

Evidence tags used below:
- **seen**: the id, label or text appears literally in S2, S3 or S4.
- **inferred**: deduced from ids, inputs and states, or from CSS. It has not been observed rendered.

## Catalog: Quant Power screens and components vs Matrix

Paths below are relative to `railway-service/src/tripity_experiment/web/`. Matrix status:
- **yes**: equivalent behaviour exists.
- **partial**: something close exists but differs.
- **no**: absent.

| # | QP screen / component | What it shows | Controls (from S3 inputs/state) | Evidence | Matrix | Matrix reference / note |
|---|---|---|---|---|---|---|
| 1 | **App shell / navigation** | Sidebar with "Quant Power" home link; nav: OI Volume, GEX Analysis, Key Levels, P/C Ratio, How to use GEX; separate Disclaimer link | URL routing (`url.pathname`, `url.search` → `page-content`) | seen (S2, S3) | partial | Sidebar views exist: `matrix.html:12-23`, `setView` `matrix.js:2342`. Matrix does **not** encode the view or the symbol in the URL, so there are no deep links (grep finds no `location.hash` or `pushState`) |
| 2 | **GEX parameter chart** (`options-chart`) | Per-strike chart of the selected parameters for one ticker and one expiry date | `ticker-input` + `search-button` (or Enter); `date-dropdown` (options filled per ticker); toggle buttons **Net GEX, AG, Call OI, Put OI, Call Vol, Put Vol, Power Zone** → `selected-params` (multi-select; active button gets class `active`) | seen ids (S3), button colours (S4) | **yes** | Same buttons: `matrix.html:136-144`, `METRICS` `matrix.js:1450-1463`, drawn in `drawChart` `matrix.js:1567`. Matrix adds AVG Power Zone, Weighted, a sigma range, and DEX/VEX/CHEX bars (`matrix.html:157-160`). Expiry is a multi-select picker (`matrix.html:46-52`), not a single date dropdown |
| 3 | **Price chart + simplified price chart** (`price-chart`, `price-chart-simplified`) | Two price charts for the searched ticker, one full and one "simplified". They do not depend on the expiry date | `ticker-input` / `search-button` | seen ids (S3); content inferred | partial | Intraday candles appear only in the Market Structure view (`drawMarketPriceCanvas` `matrix.js:2012`). The GEX view has no price chart beside the strike chart, and there is no "simplified" variant |
| 4 | **Options data store** (`options-data-store`) | Cached chain for ticker + date that feeds `options-chart` | ticker, date | seen (S3) | yes | Matrix computes in the browser from the loaded chain (`matrix.js:196-298`) |
| 5 | **OI Volume page** (`/oi-volume`): `oi-volume-chart` | Per-strike OI/volume chart | `ticker-input-oi-volume` + `search-button-oi-volume`; `date-dropdown-oi-volume`; buttons **Volume Spread, Call OI, Put OI, Call Vol, Put Vol** → `selected-params-oi-volume` | seen (S2, S3, S4) | partial | Call/Put OI and Volume are overlay buttons on the GEX chart (`matrix.html:138-141`). There is **no Volume Spread** metric and no separate OI/Volume page |
| 6 | **OI Volume price chart** (`oi-volume-price-chart`) | Price chart on the OI page. Reads both ticker and date, so it probably overlays OI/volume levels on price | ticker, date (as state) | seen id (S3); content inferred | partial | The closest is Market Structure's price chart with Call Vol / Put Vol strike lines (`marketStructureLevels` `matrix.js:1990-2006`) |
| 7 | **Key Levels page** (`/key-levels`): `key-levels-chart` | "Identification of key support and resistance levels based on GEX profile" (S2 card) | `ticker-input-key-levels` + `search-button-key-levels` | seen (S2, S3) | partial | Key Levels tiles: Spot, Zero Gamma, Max Gamma, Call Wall, Put Wall, Max Pain (`matrix.js:429-436`, panel `matrix.html:375`). Levels drawn on price: P1/N1, Call/Put Vol, Matrix Power, AG (`matrix.js:1998-2005`). No single-ticker "key levels chart" page |
| 8 | **Forecast text** (`forecast-text`, styled `#market-forecast`) | A text market forecast for the key-levels ticker. The CSS styles h3/h4/h5 headings inside it, so it is a structured multi-section report | same ticker input as Key Levels | seen id (S3), structure (S4); wording inferred | partial | Rule-based Market Read and Scenario Ladder text (`matrix.js:400-441`, rendered `matrix.js:444-490`, panels `matrix.html:368-394`). The method and content of QP's text are unknown |
| 9 | **P/C Ratio page** (`/options-summary`): `options-summary-table` | A DataTable. Its only input is `url.pathname`, so it is computed on page load with **no ticker control**, most likely a fixed multi-ticker put/call summary | none (page load) | seen (S2 nav label, S3); content inferred | partial | Matrix shows PCR for the current symbol only (`matrix.js:240`, `matrix.js:458`). There is no cross-ticker summary table |
| 10 | **GEX board table** (`gex-options-board`) | DataTable of options/GEX data for one ticker over the selected expiries | pattern-matched **expiry buttons** (`gex-expiry-btn`, multi-select → `gex-selected-dates`), **Select all** / **Clear all** (`gex-select-all-btn`, `gex-clear-all-btn`), pattern-matched **ticker buttons** (`gex-ticker-btn` → `gex-selected-ticker`) | seen (S3); table columns unknown | partial | No per-strike options table in the GEX view. The nearest is the Greek Heat Map table, strike × GEX/DEX/VEX/CHEX (`matrix.js:1842-1845`) |
| 11 | **GEX board spot info** (`gex-board-spot-info`) | Spot price line or label for the board's ticker | (from board callback) | seen (S3) | yes | Data Health "Live spot" (`matrix.html:106`), Market Read stats (`matrix.js:452-461`) |
| 12 | **Expiry toggle buttons + Select all / Clear all** | One button per expiry, multi-select | see #10 | seen (S3) | partial | Expiry checkbox picker with 0DTE / Week / All quick buttons (`matrix.html:46-52`, `applyExpirationQuickSelect` `matrix.js:2330`). There is no explicit "Clear all" |
| 13 | **Ticker toggle buttons** | Buttons for a fixed set of tickers that switch the board | see #10 | seen (S3) | partial | Search box plus favourite chips (`matrix.html:36-41`, `renderSymbolFavorites` `matrix.js:2175`). Matrix has no fixed one-click ticker row |
| 14 | **Gamma total chart** (`gamma-total-chart`) | Total gamma for the selected ticker, built from the board data store | board data + ticker; expand button | seen id (S3); content inferred | partial | The Net GEX by strike chart (`gexChart`, `matrix.html:162`) and the total in Market Read (`matrix.js:457`). It is unclear whether QP's chart is per strike or per expiry |
| 15 | **Gamma split chart** (`gamma-split-chart`) | Gamma split. The most likely reading is call gamma vs put gamma (or split by expiry) for the selected dates | selected dates + ticker; expand button | seen id (S3); content inferred | no | Matrix computes `callGex`/`putGex` per strike (`matrix.js:205-208`) and only exposes their sum (AG, `matrix.js:1455`) and `callGexPct` (`matrix.js:287`). It has no split chart |
| 16 | **GEX price chart** (`gex-price-chart`) | Price chart with GEX-derived levels for the selected dates and ticker | selected dates + ticker; expand button | seen id (S3); content inferred | partial | Market Structure "Price + Options Power Profile" (`matrix.js:2012-2076`, `matrix.html:285`). Wall and zero-gamma lines appear on the strike chart (`matrix.js:672-674`) |
| 17 | **Fullscreen toggles** (`*-expand-btn` → `*-fullscreen` store → clientside style on `*-container`) | Each of the three board charts can be expanded to full screen | expand button per chart | seen (S3: 3 server + 3 clientside callbacks) | no | Matrix has no fullscreen or expand control (grep finds no `fullscreen` or `requestFullscreen`) |
| 18 | **How to use GEX page** (`/how-to-use-gex`) | Static education page with h2/h3 headings, lists, **tables** and highlighted terms | none | seen (S2 nav, S4 `.how-to-use-gex-page` incl. `table`) | no | Matrix has short notes only, for example the Max Pain explainer (`matrix.html:479`). There is no GEX guide |
| 19 | **Disclaimer page** (`/disclaimer`) | Static legal text ("Information is provided for educational purposes only", S2 footer) | none | seen (S2) | partial | There is only a "Disclaimer" label in the header (`matrix.html:25`), with no page or text |
| 20 | **"Quant Power" magnet level** (login card) | "Unique development that identifies the magnet level that attracts price" | none known: no dedicated id in S3. Probably drawn inside an existing chart, or it is the Power Zone button | seen text (S2); placement inferred | partial | "Matrix Power" strike line (strongest Power Zone near spot, `matrix.js:1996`, `matrix.js:2003`), AVG Power Zone (`matrix.js:1529-1566`), Max Pain (`matrix.js:1161`), Max Gamma (`matrix.js:432`) |
| 21 | **Power Zone** (button and login card) | "Unique development that identifies the range within which price will move" | `btn-power-zone` (yellow `#e8ca01`) | seen (S2, S3, S4) | partial | `powerZone` per strike (`matrix.js:221-229`, button `matrix.html:142`). This is **our own guess** at the formula. QP calls it a *range*, while Matrix plots a per-strike score and a single average strike, not a range band |
| 22 | **Power Range** (`#btn-power-range`, colour `#0090ea`) | Unknown. It is styled in CSS but absent from the callback graph, so it is probably a removed or hidden button, perhaps the range form of Power Zone | none live | seen in S4 only | no | — |
| 23 | **Monitoring** (login card) | "Real-time tracking of changes" | **none in S3**: no Interval, all callbacks are click-driven | seen text (S2); implementation unknown | partial | Auto-refresh selector (`matrix.html:66-73`, `setupAutoRefresh` `matrix.js:2811`), 1 s live spot poller (`matrix.js:2819-2830`), GEX Replay (`matrix.html:116-123`), Net Drift & Flow view. Matrix does **not** show "what changed since the last read" |
| 24 | **Gamma Analysis / Volume & OI / Time is Money / Simplicity** (login cards) | Marketing descriptions of #2, #5, #10-16 and of the overall UX | — | seen (S2) | n/a | Covered by the rows above |
| 25 | **Login / access** | Username-only "CHECK ACCESS"; result kept in browser storage; "Get access" goes to Telegram | `username-input`, `submit-button` | seen (S2, S3) | n/a | Out of scope (map #21, "Out of scope") |

Per-button colours from S4, for reference only (we keep our own palette): Net GEX `#22b5ff`, AG `#915bf8`, Call OI `#02d432`, Put OI `#f32d35`, Call Vol `#003cfe`, Put Vol `#e55f04`, Power Zone `#e8ca01`, Power Range `#0090ea`. On the OI page, Volume Spread uses the same blue as Net GEX (`#22b5ff`), which hints that it plays the "net" role on that page. Matrix already uses a close scheme (`matrix.html:136-144`).

## Metrics whose formula is unknown to us

No public source explains any of these (S6 returned nothing). The only first-party wording is what S2 and S4 contain.

| Metric | Public wording | What can be inferred | Matrix today |
|---|---|---|---|
| **Power Zone** | "Unique development that identifies the range within which price will move" (S2) | It is a *range*, not just a strike, and it is selectable as an overlay alongside Net GEX/AG/OI/Vol on the per-strike chart (S3) | Own heuristic `0.58·AG + 0.28·OI + 0.14·Vol` normalised (`matrix.js:228`). It is not QP's formula and does not output a range |
| **Quant Power (magnet level)** | "Unique development that identifies the magnet level that attracts price" (S2) | A single price level, with no dedicated component id, so it is probably an annotation on an existing chart | Approximated by Matrix Power / AVG Power Zone / Max Pain (see row 20) |
| **Power Range** | none (CSS only) | Possibly an earlier or alternate range output of Power Zone | — |
| **Volume Spread** | none | First button on the OI page, coloured like Net GEX. Most likely net volume per strike (call vol − put vol) or call/put volume imbalance. **Unverified** | absent |
| **AG** | none | Probably "absolute gamma" (`|call GEX| + |put GEX|`), which is how Matrix reads it (`matrix.js:1455`). **Unverified** | present (own definition) |
| **Forecast text** | none | Multi-section text (h3/h4/h5 headings) generated from the key-levels ticker. It is either rule-based or written by hand | Rule-based Market Read (`matrix.js:400-441`) |
| **Key levels** | "Identification of key support and resistance levels based on GEX profile" (S2) | Which levels are used (walls, flip, max gamma, …) is unknown | Spot, Zero Gamma, Max Gamma, Call/Put Wall, Max Pain (`matrix.js:429-436`) |
| **P/C Ratio summary** | nav label only | Whether it uses OI or volume, which tickers, and which expiries are all unknown | single-symbol OI-based PCR (`matrix.js:240`) |
| **Gamma total / Gamma split** | "Detailed analysis of gamma exposure to understand market makers' actions" (S2) | The axes (strike vs expiry) and the split dimension (call/put vs expiry) are unknown | Net GEX by strike only |

## Open questions

1. **Which route hosts which work area?** The parameter-button page (`ticker-input`, `date-dropdown`, `options-chart`) and the GEX board (`gex-*`) could be `/` and `/gex-analysis` in either order. S3 does not say. Does it matter for us, or do we keep Matrix's own view names?
2. **What does `url.search` carry?** Probably a ticker or date query parameter for deep links. Should Matrix adopt URL deep links (row 1)?
3. **Power Zone as a range.** Should Matrix turn its per-strike score into a band (low/high) to match the "range within which price will move" behaviour? If so, what rule (for example, the contiguous strikes around spot holding X% of Power Zone mass)? This is a decision, not research.
4. **Volume Spread definition.** Should we adopt call − put volume per strike (our own choice), or leave it out?
5. **Gamma split dimension.** Should it be call vs put, or per expiry? Pick one, or offer both behind a toggle.
6. **GEX board columns.** S3 does not expose the table's columns. Should Matrix design its own strike table (for example strike, call/put OI, call/put GEX, net GEX per selected expiry)?
7. **P/C summary universe.** Which tickers, and should it use OI or volume PCR? Matrix's on-demand symbols are excluded from history collection (README), so a summary table would cover the four core symbols, or would need a batch fetch within Cboe and LSE limits.
8. **Monitoring.** QP shows no polling in its callback graph. Matrix already polls spot every 1 s and refreshes the chain on a timer. What should "tracking of changes" mean for us, for example a diff of key levels or walls since the last refresh, or alerts? This depends on the LSE 60-calls-per-minute budget (map #21, "Not yet specified").
9. **Fixed ticker row vs favourites.** Is a one-click NDX/SPX/SPY/QQQ row wanted in addition to search and favourites?
10. **Education content.** A "How to use GEX" page would be our own writing. Who writes it, and in which language (UI is English, users likely Hebrew-speaking)?
11. **Public evidence is exhausted.** No screenshots, videos or posts exist publicly. Any further detail (rendered charts, the forecast wording, the table columns) would need someone with legitimate access, which is out of scope.
