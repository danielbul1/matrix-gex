# Quant Power: design, structure and how the screens work (third pass)

Research for "be like him" (follow-up to map #21). Question: what does Quant Power look like and how is it wired, as far as public sources and the trader's own screenshots show? Builds on `quant-power-catalog.md`, `quant-power-formulas.md` and `gex-dashboard-comparison.md`; facts already there are not repeated.

We copy behaviour and look, not code, name, branding or data. Nothing behind his login was touched and `/_dash-update-component` was never called.

## Sources (2026-10-26)

| # | Source | What it gave |
|---|---|---|
| S1 | `/`, `/_dash-layout`, `/_dash-dependencies`, `/assets/styles.css` on both hosts (`maxpowertrading` and `151212aa`) | Unchanged since the earlier passes: the same 24 callbacks, and a byte-identical `styles.css` (28,610 bytes) last modified 2026-09-07. The only difference between the hosts is the Telegram link |
| S2 | Three video frames the trader sent (an SPY Strike Chart over his TradingView chart, and the GEX board) | The strongest new evidence; see below |
| S3 | `vlasenkoalexey/gex_dashboard` (all three commits) and `gex_weekly_strategy_validator` (to `3569449`) | Read only. The board header template and highlight colour match his, so the follower modelled the board on his |
| S4 | Other public GEX projects | None names Quant Power. One TradingView script uses his level names; see the last section |

## Design system

Most of `styles.css` styles a login page and classes the live app does not use. The app's real shell is inline.

- **Page:** flat `#1a1a1a` (the last `body` rule wins over an earlier gradient). Font `'Inter', sans-serif`, but Inter is never loaded, so the system sans shows.
- **Sidebar:** fixed, 10% wide, `#191919`, shadow `5px 0 15px rgba(0,0,0,.2)`, 20px padding. A white bold brand over a 2px rule at 50% white, then plain white links 20px apart with no hover or active style. "Disclaimer" in grey, 20px, at the bottom under its own rule. The content area starts at 10%, 20px padding.
- **Accent:** a cyan-to-blue gradient, `#00ffcc` → `#008cff`. It fills the Search button (dark `#1e1e1e` bold text, 8px corners, cyan glow; hover `#66ffd9` → `#00b894`, lifts 1px) and colours headings as gradient text (h1, How-to h2 and table headers, Forecast h3 and h5). How-to h3 is `#66ffd9`.
- **Controls:** inputs and dropdowns `#3a3a3a`, no border, 6px corners, 10px padding, 16px white. The control box is an 800px centred card, `#1a1a1a`, 8px corners, 15px padding. Parameter buttons as already documented.
- **Surfaces:** charts sit on `#1e1e1e`, 12px corners, 10px padding, shadow `0 4px 10px rgba(0,0,0,.7)`. Text pages (Key Levels, P/C Ratio, How to use GEX, Disclaimer) share one card: `#1a1a1a`, 1px `#2a2a2a` border, 12px corners, 2.5rem padding. The Forecast is the same card under the chart, 2rem in.
- **Scrollbars:** 6px, thumb `#2a2a2a`; the board's table 5px, thumb `#555` (hover `#777`).

## Strike Chart (S2, first frame)

- **Expiration** is a multi-select dropdown: chips with ×, a clear-all × and an arrow. The frame has a whole week chosen (2026-09-14 to 09-18), summed per strike.
- **Parameters** label above the buttons; the buttons read Net GEX, AG, Call OI, Put OI, **Call Volume**, **Put Volume**, Power Zone.
- **Window:** SPY 742 to 787 around 764.29, about ±3% (46 strikes). Earlier thumbnails give SPX about ±1.1%.
- **Chart:** no gridlines; a small "SPY" at the top left; a small legend at the top right; the left axis titled "Net GEX"; every strike labelled under the x axis, titled "Strike"; a large grey watermark; a thin orange price line labelled "Price: 764.29".
- **Units (open):** the Net GEX axis runs from about +200k to −800k, while his board's Net GEX cells for SPY are in the hundreds of millions, and the AG axis reaches 50M. The Strike Chart therefore seems to use a different scale from the board (roughly without the S²·1% factor). Not acted on.

## GEX board (S2, third frame)

- **Layout:** a "Select Ticker:" card with SPX, SPY and QQQ (no NDX); the active one a lighter grey. Under it one card: "Options Chain Board", then the line "Spot: 767.18 | Total Gamma: $-1.39 Bn | Regime: Negative | Ticker: SPY".
- **Expiry buttons:** a grid, eight per row, each with the date over a percentage. The percentages add up to 100.00%. Far dates are tiny (2029-01-19: 0.02%) and the monthly is large (2026-10-16: 19.69%), so the base is gamma, not open interest (inferred). Chosen buttons are light steel blue with dark text. The frame has the current week chosen (2026-09-28 to 10-02). Select All and Clear All sit under the grid.
- **Table:** headers in capitals: STRIKE, NET GEX, ABS GEX, NOTIONAL DELTA, TOTAL OI, CALL OI, PUT OI, TOTAL VOL, CVOL, PVOL, %. Whole numbers with separators. Net GEX and % blue or red by sign; ABS GEX, CALL OI and CVOL blue; PUT OI and PVOL red; TOTAL OI bold.
- **Highlights:** a green dot after each column's largest value, and every row holding one shaded green-grey (`#647865` in the follower's copy). Net GEX counts by size: its dot sits on −906,337,011. The spot row is not highlighted.
  - Checked against his SPY frame (2026-10-27): 768 holds the cVol maximum, 765 Total Vol and pVol, 761 Net GEX, Abs GEX, Notional Delta and Put OI. No row is shaded without a dot, so the rule is "largest in some column", not a statistical outlier test. The single lit cell at 770 cVol (34,053, below 768's 36,441) is most likely the cell he clicked.
  - gex_dashboard differs: it shades whole rows only at key levels (gamma flip, call wall, put wall, max AG, max and min Net GEX) and lights a column maximum as one cell, with no dots. His frame matches neither its key-level rows (767/768 flip area is not shaded unless it holds a maximum) nor its single-cell maxima.
- **% check:** −906,337,011 → −6.62% and 126,640,034 → +0.92% give the same base, about $13.7 Bn, which fits the sum of ABS GEX.
- **Charts** (480px column, each with ⛶):
  - "Net Gamma: $-1.39 Bn per 1% SPY Move": Net Gamma bars (blue up, red down) and an Abs GEX purple line with markers and fill on its own scale from the bottom; y axis "Gamma Exposure ($ billions / 1% move)"; gridlines; an orange "Spot: 767.18" line; a strike range much wider than the table's.
  - "Gamma Split - Total: $-1.39 Bn per 1% SPY Move": Call Gamma green above zero, Put Gamma red below, and the same Abs GEX line.
  - A third chart, cut off in the frame.

## His TradingView chart (S2, first frame)

Behind the Strike Chart is a TradingView SPY chart with ranked levels at the right edge: P1 775, P2 779, P3 770, N1 760, N2 750, N3 755, and AG2 750 (AG1 seemingly 760). Support, resistance, G-flip and σ bands come from another indicator. The levels are not on his site; how he gets them there is unknown. Deferred by the trader until the site matches his.

## Similar projects

- **"GEX Levels v3 [Barrett]"** (TradingView, open source, 2026-09-16): the only public trace of his level names (P1, P2, N1, N2, AG1, AG2, plus Flip, Max Pain, expected move, walls). Levels arrive as one pasted string in three blocks (0DTE, weekly, monthly) computed off-platform. Likely another follower.
- **Darthreign/gex-dashboard** (Plotly Dash, like his): exports levels to TradingView as `price,label,kind;…`.
- **EazyDuz1t/EzOptions-Schwab**: Abs GEX as a translucent area behind the Net GEX bars.
- **simonro/Edge-GEX-Lab**: template-built guidance text from map levels, a model for the Forecast.

## What Matrix took from this pass

The sidebar, Search button, inputs, cards and gradient headings; the multi-date Expiration picker starting on the current week; the Strike Window by ticker; no gridlines on the Strike Chart; the board's layout, labels, colours, highlights, Expiry Shares, SPX/SPY/QQQ row, and its Net Gamma and call/put Gamma Split charts with an Abs GEX line over ±10%.
