"""Public host for Tripity company connectors: create page + /mcp/{company} + OAuth."""

from __future__ import annotations

import html
import ipaddress
import asyncio
import csv
import datetime
import gzip
import io
import json
import os
import re
import socket
import time
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
import uvicorn

from tripity_experiment.company_api import infer_api_base_url
from tripity_experiment.har_openapi import har_to_openapi
from tripity_experiment.company_connector import build_company_connector_draft
from tripity_experiment.connector_manifest import manifest_from_draft
from tripity_experiment.mcp_adapter import build_mcp_server
from tripity_experiment import matrix_gex
from tripity_experiment.openapi_intake import IntakeError, parse_openapi_text
from tripity_experiment.project_gateway import BearerGate
from tripity_experiment.semantic_curation import apply_tool_curation_to_spec
from tripity_experiment.source_analysis import analyze_source_url

DEFAULT_OPENAPI_URL = "https://petstore3.swagger.io/api/v3/openapi.json"
DEFAULT_COMPANY_NAME = "Swagger Petstore"
MATRIX_SPX_QUOTE_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_SPX.json"
MATRIX_OPTIONS_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{}.json"
MATRIX_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
# LSE Data 3.0 (the /vault and x_* calls are gone: 410/404 since 2026-10).
# Free plan: 60 calls a minute and one export download at a time per key.
MATRIX_LSE_V3_URL = "https://download.londonstrategicedge.com/v3"
MATRIX_LSE_CHAIN_URL = f"{MATRIX_LSE_V3_URL}/options/chain"
MATRIX_LSE_PREVIEW_URL = f"{MATRIX_LSE_V3_URL}/preview"
MATRIX_LSE_EXPORT_URL = f"{MATRIX_LSE_V3_URL}/export"
MATRIX_LSE_SYMBOLS = ("SPY", "QQQ")
# display symbol -> (LSE v3 market, LSE v3 symbol) for REST prices and candles.
MATRIX_LSE_PRICE_SYMBOLS = {
    "SPX": ("indices", "SPX"),
    "NDX": ("indices", "NDX"),
    "SPY": ("us_stocks", "SPY"),
    "QQQ": ("us_stocks", "QQQ"),
}
MATRIX_LSE_TIMEFRAMES = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "1h"}
MATRIX_LSE_RANGE_DAYS = {"1d": 4, "5d": 8, "1mo": 32}
MATRIX_LSE_CANDLE_MAX_AGE_SECONDS = 4 * 24 * 3600  # spans a weekend plus a holiday
MATRIX_YAHOO_SYMBOLS = {"SPX": "^GSPC", "NDX": "^NDX", "VIX": "^VIX"}
MATRIX_CANDLE_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m"}
MATRIX_CBOE_SYMBOLS = (
    ("_NDX", "NDX"),
    ("_SPX", "SPX"),
    ("SPY", "SPY"),
    ("QQQ", "QQQ"),
    ("_VIX", "VIX"),
)
MATRIX_CORS_ORIGINS = {
    "https://danielbul1.github.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
}
MATRIX_OCC = re.compile(r"([A-Z]+)(\d{6})([CP])(\d{8})")
MATRIX_STRIKE_RANGE = 0.25
MATRIX_CBOE_DATA_CACHE_SECONDS = 120.0
MATRIX_DYNAMIC_CHAIN_CACHE_MAX = 64
MATRIX_DYNAMIC_CHAIN_FETCHES_PER_MINUTE = 12
MATRIX_MARKET_DATA_CACHE_SECONDS = 300.0
MATRIX_CANDLES_CACHE_SECONDS = 50.0
MATRIX_CANDLES_CACHE_MAX = 256  # any ticker can be asked for, so keep the cache bounded
_MATRIX_PACKAGED_WEB_DIR = Path(__file__).with_name("web")
MATRIX_WEB_DIR = (
    _MATRIX_PACKAGED_WEB_DIR
    if (_MATRIX_PACKAGED_WEB_DIR / "matrix.html").exists()
    else Path.cwd() / "src" / "tripity_experiment" / "web"
)
# Every Matrix screen has its own path; all of them serve the same matrix.html,
# which picks the screen from the path (map #21, "בנייה 2").
MATRIX_SCREEN_PATHS = frozenset(
    "/matrix" + ("/" + screen if screen else "")
    for screen in ("", "oi-volume", "gex-analysis", "key-levels", "options-summary", "how-to-use-gex", "disclaimer")
)
_matrix_cboe_data_cache: tuple[float, dict[str, Any]] | None = None
_matrix_cboe_data_lock = asyncio.Lock()
_matrix_dynamic_chain_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_matrix_dynamic_chain_lock = asyncio.Lock()
_matrix_dynamic_chain_fetches: list[float] = []
_matrix_market_data_cache: tuple[float, dict[str, Any]] | None = None
_matrix_market_data_lock = asyncio.Lock()
_matrix_candles_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_matrix_candles_lock = asyncio.Lock()
_matrix_lse_spots_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
_matrix_lse_spots_lock = asyncio.Lock()
_matrix_lse_export_lock = asyncio.Lock()
LANDING_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Tripity — AI connectors for company apps</title>
  <meta name="description" content="Turn your company API or app into a secure ChatGPT and Claude connector." />
  <style>
    :root{--bg:#080b12;--line:#243044;--text:#f8fafc;--muted:#a7b0c0;--brand:#8b5cf6;--brand2:#22d3ee;--ok:#34d399}
    *{box-sizing:border-box} body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(circle at 20% 0%,#1e1b4b 0,#080b12 36%),var(--bg);color:var(--text);letter-spacing:-.01em}
    a{color:inherit;text-decoration:none}.wrap{max-width:1120px;margin:0 auto;padding:0 22px}.nav{display:flex;align-items:center;justify-content:space-between;padding:22px 0}.logo{font-weight:800;font-size:20px}.pill{display:inline-flex;gap:8px;align-items:center;border:1px solid #334155;background:#0b1220cc;border-radius:999px;padding:8px 12px;color:#cbd5e1;font-size:13px}.navlinks{display:flex;gap:18px;color:#cbd5e1;font-size:14px}.btn{display:inline-flex;align-items:center;justify-content:center;border-radius:12px;padding:12px 16px;font-weight:700;background:white;color:#080b12;border:1px solid white}.btn.secondary{background:#111827;color:white;border-color:#334155}.hero{padding:72px 0 46px;text-align:center}.hero h1{font-size:clamp(44px,7vw,82px);line-height:.95;margin:20px auto;max-width:930px}.grad{background:linear-gradient(90deg,var(--brand2),#fff,var(--brand));-webkit-background-clip:text;background-clip:text;color:transparent}.hero p{font-size:clamp(18px,2.4vw,23px);line-height:1.55;color:var(--muted);max-width:760px;margin:0 auto 28px}.ctas{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}.proof{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:34px 0}.proof div,.card{background:linear-gradient(180deg,#111827dd,#0b1020dd);border:1px solid var(--line);border-radius:20px;box-shadow:0 24px 80px #0006}.proof div{padding:16px;text-align:left}.proof b{display:block;font-size:20px}.proof span{color:var(--muted);font-size:13px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:28px 0}.card{padding:24px}.card h3{margin:0 0 10px;font-size:20px}.card p,.section p{color:var(--muted);line-height:1.6}.section{padding:42px 0}.section h2{font-size:36px;margin:0 0 12px}.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#050816;border:1px solid var(--line);border-radius:16px;padding:18px;color:#dbeafe;overflow:auto;white-space:pre-line}.split{display:grid;grid-template-columns:1.05fr .95fr;gap:18px;align-items:stretch}.check{color:var(--ok);font-weight:800}.footer{padding:34px 0 50px;color:#64748b;font-size:14px;border-top:1px solid #1f2937;margin-top:30px}@media(max-width:800px){.proof,.grid,.split{grid-template-columns:1fr}.navlinks{display:none}.hero{text-align:left}.ctas{justify-content:flex-start}}
  </style>
</head>
<body>
  <div class="wrap">
    <nav class="nav"><div class="logo">Tripity</div><div class="navlinks"><a href="#how">How it works</a><a href="#safety">Safety</a><a href="/create">Create</a></div></nav>
    <section class="hero">
      <span class="pill">Live on Railway · OAuth MCP · ChatGPT tested</span>
      <h1>Give your company app <span class="grad">an AI link.</span></h1>
      <p>Tripity turns approved company APIs, docs, or browser traffic into secure ChatGPT/Claude connectors. OpenAPI if you have it. Assisted connector build if you don’t.</p>
      <div class="ctas"><a class="btn" href="/create">Create a connector</a><a class="btn secondary" href="/create#assisted">Request assisted setup</a></div>
    </section>
    <section class="proof">
      <div><b>OAuth</b><span>Public MCP connection for AI clients</span></div>
      <div><b>Read-only first</b><span>Writes require explicit approval</span></div>
      <div><b>No passwords</b><span>Official/approved sources only</span></div>
      <div><b>Live proof</b><span>Petstore, XKCD, HAR-to-MCP tested</span></div>
    </section>
    <section id="how" class="section">
      <h2>Three paths to the same outcome</h2>
      <p>A hosted MCP URL your AI tools can use.</p>
      <div class="grid">
        <div class="card"><h3>1. OpenAPI</h3><p>Paste an OpenAPI URL. Tripity curates safe read tools and returns a public MCP link.</p></div>
        <div class="card"><h3>2. Auto-discovery</h3><p>Paste a website or API docs URL. Tripity checks common OpenAPI locations automatically.</p></div>
        <div class="card"><h3>3. No OpenAPI</h3><p>Use approved HAR traffic or request assisted setup. Tripity drafts the API connector for review.</p></div>
      </div>
    </section>
    <section class="section split">
      <div class="card"><h2>What you get</h2><p><span class="check">✓</span> Public MCP URL like <code>/mcp/acme</code></p><p><span class="check">✓</span> OAuth-compatible connector flow</p><p><span class="check">✓</span> Curated tools with readable names</p><p><span class="check">✓</span> Activity metadata without secret payload logging</p></div>
      <div class="code">Company app/API
  → Tripity intake
  → Draft or imported OpenAPI
  → Safety classifier
  → Curated MCP tools
  → ChatGPT / Claude / Cursor</div>
    </section>
    <section id="safety" class="section">
      <h2>Built for approved company sources</h2>
      <p>Tripity is not a scraper for random websites. It is a connector service for apps and APIs you own or have permission to test. Read-only by default. No end-user passwords. Human approval before writes.</p>
    </section>
    <section class="section card" style="text-align:center"><h2>Try the live connector builder</h2><p>Use the prefilled Petstore demo, paste your OpenAPI URL, or request an assisted build.</p><div class="ctas"><a class="btn" href="/create">Open builder</a></div></section>
    <footer class="footer">Tripity — done-for-you AI connectors for company apps.</footer>
  </div>
</body>
</html>"""

CREATE_PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Tripity - Create AI Connector</title>
  <style>
    :root{--bg:#f8fafc;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--card:#fff;--brand:#111827;--ok:#047857;--err:#b91c1c}
    *{box-sizing:border-box} body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;max-width:980px;margin:0 auto;padding:32px 18px 70px;color:var(--ink);background:radial-gradient(circle at top,#e0f2fe 0,#f8fafc 38%)}
    .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:38px}.logo{font-weight:850;font-size:20px} a{color:inherit}.card{background:var(--card);border:1px solid var(--line);border-radius:24px;padding:26px;box-shadow:0 18px 60px #0f172a12;margin:18px 0}.hero{text-align:center;padding:28px 0}.hero h1{font-size:clamp(38px,6vw,68px);line-height:.98;margin:12px auto;max-width:820px;letter-spacing:-.05em}.hero p{font-size:20px;color:var(--muted);line-height:1.55;margin:0 auto;max-width:700px}.builder{max-width:760px;margin:24px auto}.inputrow{display:grid;grid-template-columns:1fr auto;gap:10px;margin-top:18px}input,textarea,button{font:inherit}input,textarea{width:100%;padding:15px 16px;border:1px solid #cbd5e1;border-radius:14px;background:white}button,.btn{background:var(--brand);color:white;border:0;border-radius:14px;padding:15px 18px;font-weight:800;cursor:pointer;text-decoration:none;display:inline-flex;justify-content:center}.muted{color:var(--muted)}.ok{color:var(--ok)}.err{color:var(--err)}pre{background:#0b1020;color:#dbeafe;border-radius:16px;padding:16px;overflow:auto;white-space:pre-wrap}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.mini{padding:18px;border:1px solid var(--line);border-radius:18px;background:#fff}.mini b{display:block;margin-bottom:5px}details{border-top:1px solid var(--line);padding-top:18px;margin-top:20px}summary{cursor:pointer;font-weight:800}.result{display:none}.lead{display:none}.small{font-size:13px}@media(max-width:760px){.inputrow,.grid{grid-template-columns:1fr}.hero{text-align:left}.top{align-items:flex-start}}
  </style>
</head>
<body>
  <div class="top"><div class="logo">Tripity</div><a class="muted" href="/">Home</a></div>
  <section class="hero">
    <p class="small">AI connector builder</p>
    <h1>Paste your app or API docs. Get an AI link.</h1>
    <p>Tripity tries to find the safest connector path automatically. If we can build it instantly, you get a ChatGPT/Claude MCP URL. If not, we save it for assisted setup.</p>
  </section>

  <section class="card builder">
    <label><b>What do you want to connect?</b></label>
    <div class="inputrow">
      <input id="url" value="https://petstore3.swagger.io" placeholder="https://your-app.com or https://docs.company.com" />
      <button id="create">Create AI link</button>
    </div>
    <p class="muted small">Use an app/API you own or have permission to test. Sample is prefilled.</p>
    <div id="status" class="muted"></div>

    <div class="result" id="result">
      <h2 class="ok">AI link ready</h2>
      <p><b>MCP URL for ChatGPT/Claude:</b></p>
      <pre id="mcp"></pre>
      <button id="copy" type="button">Copy link</button>
      <p><b>Safe tools found:</b></p>
      <pre id="tools"></pre>
    </div>

    <div class="lead" id="lead">
      <h2>We need assisted setup</h2>
      <p class="muted">This app did not expose enough public API information for an instant connector. Send your email and what you want AI to answer — this is the normal path for private apps.</p>
      <input id="leadEmail" placeholder="you@company.com" />
      <textarea id="leadQuestions" rows="4" placeholder="Example: Find customer orders, summarize tickets, check inventory..."></textarea>
      <button id="requestHelp" type="button">Request assisted build</button>
      <div id="leadStatus" class="muted"></div>
    </div>

    <details>
      <summary>Advanced: I have an approved browser/API capture</summary>
      <p class="muted small">For technical testers only. Paste HAR JSON from an app you own or have permission to test. Tripity strips headers/cookies and drafts a read-only connector from observed API calls.</p>
      <textarea id="har" rows="7" placeholder='Paste HAR JSON with {"log":{"entries":[...]}}'></textarea>
      <button id="createHar" type="button">Create from capture</button>
    </details>
  </section>

  <section class="grid">
    <div class="mini"><b>Instant when possible</b><span class="muted">Finds OpenAPI automatically from common locations.</span></div>
    <div class="mini"><b>Private apps are normal</b><span class="muted">If public discovery fails, assisted setup captures the real need.</span></div>
    <div class="mini"><b>Safe by default</b><span class="muted">Approved sources only. Read-only first. No passwords.</span></div>
  </section>

<script>
const $ = (id) => document.getElementById(id);
function normalizedUrl(){ const raw=$('url').value.trim(); return (raw.startsWith('http://') || raw.startsWith('https://')) ? raw : 'https://' + raw; }
function companyFromUrl(){ try { return new URL(normalizedUrl()).hostname.replace(/^www\\./,''); } catch(e) { return 'Company'; } }
async function showMcp(url, toolsText) {
  $('status').textContent = 'Done.'; $('status').className = 'ok';
  $('mcp').textContent = url;
  $('tools').textContent = toolsText;
  $('copy').onclick = () => navigator.clipboard.writeText(url);
  $('result').style.display = 'block';
}
async function createConnector(payload) {
  $('status').textContent = 'Working... checking for existing MCP, then trying automatic discovery.';
  $('status').className = 'muted';
  $('result').style.display = 'none';
  $('lead').style.display = 'none';
  try {
    if (payload.openapi_url) {
      const a = await fetch('/api/analyze-source', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({url: payload.openapi_url})});
      const analysis = await a.json();
      if (analysis.kind === 'existing_mcp') {
        await showMcp(analysis.mcp_url, 'Existing MCP detected. Connect this URL directly.');
        return;
      }
    }
    const r = await fetch('/api/public-connectors', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(payload)});
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    await showMcp(data.mcp_url, data.enabled_tools.join('\n'));
  } catch (e) {
    $('status').textContent = 'Instant connector not available: ' + e.message;
    $('status').className = 'err';
    $('lead').style.display = 'block';
  }
}
$('create').onclick = () => createConnector({company_name:companyFromUrl(), openapi_url:normalizedUrl()});
$('createHar').onclick = () => {let har; try{har=JSON.parse($('har').value)}catch(e){$('status').textContent='Capture must be valid JSON';$('status').className='err';return;} createConnector({company_name:companyFromUrl(), har});};
$('requestHelp').onclick = async () => {
  $('leadStatus').textContent='Saving request...'; $('leadStatus').className='muted';
  try {
    const r = await fetch('/api/assisted-setup-requests', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({email:$('leadEmail').value, app_url:normalizedUrl(), questions:$('leadQuestions').value})});
    const data = await r.json(); if(!r.ok) throw new Error(data.detail || JSON.stringify(data));
    $('leadStatus').textContent='Request saved. This is ready for assisted connector build.'; $('leadStatus').className='ok';
  } catch(e) {$('leadStatus').textContent='Error: '+e.message; $('leadStatus').className='err';}
};
</script>
</body>
</html>"""



@dataclass
class PublicConnector:
    slug: str
    company_name: str
    openapi_url: str
    api_base_url: str
    mcp_path: str
    enabled_tools: tuple[str, ...]
    disabled_tools: tuple[str, ...]
    app: Any
    openapi_spec: dict[str, Any] | None = None


async def _fetch_matrix_cboe_quote(url: str, symbol: str, cboe_symbol: str) -> dict[str, Any]:
    # Cboe moves its CDN host now and then (cdn -> cdn-api in 2026-09); follow
    # its redirects so the next move doesn't silently empty the dashboard.
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={"User-Agent": "Tripity Matrix quote proxy (+https://trytripity.site)"},
        )
        response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("CBOE quote response did not include data")
    price = data.get("current_price")
    if not isinstance(price, int | float):
        raise ValueError("CBOE quote response did not include current_price")
    asof = data.get("last_trade_time") or payload.get("timestamp")
    return {
        "ok": True,
        "symbol": symbol,
        "spot": float(price),
        "asof": asof,
        "source": "cboe_delayed_quote",
        "cboe_symbol": payload.get("symbol") or cboe_symbol,
    }


async def _fetch_matrix_spx_quote() -> dict[str, Any]:
    return await _fetch_matrix_cboe_quote(MATRIX_SPX_QUOTE_URL, "SPX", "_SPX")


def _matrix_lse_candles_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in rows:
        timestamp = row.get("time") or row.get("ts") or row.get("timestamp")
        if not timestamp:
            continue
        try:
            parsed = datetime.datetime.fromisoformat(str(timestamp).replace(" ", "T").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            values = tuple(float(row[name]) for name in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        candles.append({
            "time": int(parsed.timestamp()),
            "open": round(values[0], 4),
            "high": round(values[1], 4),
            "low": round(values[2], 4),
            "close": round(values[3], 4),
            "volume": int(float(row.get("volume") or 0)),
        })
    candles.sort(key=lambda candle: candle["time"])
    return candles


async def _fetch_matrix_candles_uncached(
    *,
    symbol: str = "SPY",
    interval: str = "1m",
    range_: str = "1d",
) -> dict[str, Any]:
    # Key Levels draws price for any ticker it can search, so any valid
    # symbol is allowed; tickers LSE does not carry come from Yahoo.
    symbol = _matrix_dynamic_symbol(symbol)
    if interval not in MATRIX_CANDLE_INTERVALS:
        raise ValueError("Unsupported candle interval")
    api_key = _matrix_lse_api_key()
    lse_pair = MATRIX_LSE_PRICE_SYMBOLS.get(symbol)
    timeframe = MATRIX_LSE_TIMEFRAMES.get(interval)
    if api_key and lse_pair and timeframe:
        market, lse_symbol = lse_pair
        today = datetime.datetime.now(datetime.timezone.utc).date()
        days = MATRIX_LSE_RANGE_DAYS.get(range_, MATRIX_LSE_RANGE_DAYS["5d"])
        params = {
            "market": market,
            "symbol": lse_symbol,
            "timeframe": timeframe,
            "data": "candles",
            "format": "csv",
            "from": (today - datetime.timedelta(days=days)).isoformat(),
            "to": today.isoformat(),
        }
        try:
            # A key may stream one export at a time; serialize ours so the
            # dashboard's parallel chart requests don't trip 429s.
            async with _matrix_lse_export_lock:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        MATRIX_LSE_EXPORT_URL,
                        params=params,
                        headers=_matrix_lse_headers(api_key),
                    )
                    response.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(response.text)))
            candles = _matrix_lse_candles_payload(rows)[-(500 if range_ == "1d" else 2500):]
            # LSE's intraday index history has gaps (SPX 1h held only two days
            # of October 2026); a stale last candle falls through to Yahoo.
            stale_before = time.time() - MATRIX_LSE_CANDLE_MAX_AGE_SECONDS
            if candles and candles[-1]["time"] >= stale_before:
                return {
                    "ok": True,
                    "symbol": symbol,
                    "interval": interval,
                    "range": range_,
                    "source": "lse_v3",
                    "asof": candles[-1]["time"],
                    "candles": candles,
                }
        except (httpx.HTTPError, ValueError, csv.Error):
            pass

    yahoo_symbol = MATRIX_YAHOO_SYMBOLS.get(symbol, symbol)
    params = {"range": range_, "interval": interval, "includePrePost": "false"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            MATRIX_YAHOO_CHART_URL.format(yahoo_symbol),
            params=params,
            headers={"User-Agent": "Tripity Matrix candle proxy (+https://trytripity.site)"},
        )
        response.raise_for_status()
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        raise ValueError("Yahoo chart response did not include result")
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    candles: list[dict[str, Any]] = []
    for i, timestamp in enumerate(timestamps):
        try:
            values = (opens[i], highs[i], lows[i], closes[i])
        except IndexError:
            continue
        if any(value is None for value in values):
            continue
        candles.append({
            "time": int(timestamp),
            "open": round(float(values[0]), 4),
            "high": round(float(values[1]), 4),
            "low": round(float(values[2]), 4),
            "close": round(float(values[3]), 4),
        })
    if not candles:
        raise ValueError("Yahoo chart response did not include candles")
    return {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "range": range_,
        "source": "yahoo_chart",
        "candles": candles,
        "asof": candles[-1]["time"],
    }


async def _fetch_matrix_candles(
    *,
    symbol: str = "SPY",
    interval: str = "1m",
    range_: str = "1d",
) -> dict[str, Any]:
    key = (symbol.upper(), interval, range_)
    now = time.time()
    cached = _matrix_candles_cache.get(key)
    if cached is not None and now - cached[0] < MATRIX_CANDLES_CACHE_SECONDS:
        return cached[1]
    async with _matrix_candles_lock:
        now = time.time()
        cached = _matrix_candles_cache.get(key)
        if cached is not None and now - cached[0] < MATRIX_CANDLES_CACHE_SECONDS:
            return cached[1]
        data = await _fetch_matrix_candles_uncached(symbol=symbol, interval=interval, range_=range_)
        if len(_matrix_candles_cache) >= MATRIX_CANDLES_CACHE_MAX:
            _matrix_candles_cache.clear()
        _matrix_candles_cache[key] = (time.time(), data)
        return data


def _parse_matrix_occ(option_symbol: str) -> tuple[str, datetime.date, str, float] | None:
    match = MATRIX_OCC.match(option_symbol)
    if not match:
        return None
    root, date_text, call_put, strike = match.groups()
    expiration = datetime.date(
        2000 + int(date_text[:2]),
        int(date_text[2:4]),
        int(date_text[4:6]),
    )
    return root, expiration, call_put, int(strike) / 1000.0


def _matrix_expiry_carries(options, spot, last_trade_time, today) -> dict:
    """{(root, expiry): implied carry} for a raw Cboe chain (#72); {} without
    a spot or quotes."""
    if not spot or not isinstance(options, list):
        return {}
    try:
        valuation_ms = datetime.datetime.fromisoformat(str(last_trade_time)).replace(
            tzinfo=matrix_gex.ET).timestamp() * 1000
    except (TypeError, ValueError):
        valuation_ms = None
    quotes = []
    for option in options:
        if not isinstance(option, dict):
            continue
        parsed = _parse_matrix_occ(str(option.get("option") or ""))
        if parsed is None or parsed[1] < today:
            continue
        root, expiration, call_put, strike = parsed
        quotes.append((root, expiration.isoformat(), call_put, strike,
                       option.get("bid"), option.get("ask")))
    return matrix_gex.expiry_carries(quotes, float(spot), valuation_ms)


def _compact_matrix_options_payload(
    display_symbol: str,
    payload: dict[str, Any],
    *,
    today: datetime.date,
) -> tuple[str, dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"CBOE options response did not include data for {display_symbol}")
    spot = data.get("current_price") or data.get("close")
    options = data.get("options")
    if not isinstance(options, list):
        raise ValueError(f"CBOE options response did not include options for {display_symbol}")
    compact_options: list[dict[str, Any]] = []
    carries = _matrix_expiry_carries(options, spot, data.get("last_trade_time"), today)
    # P/C Ratio screen: call/put OI and volume over every live expiry and every
    # strike, counted before the strike-range and zero-OI filters below.
    totals = {"call_oi": 0, "put_oi": 0, "call_vol": 0, "put_vol": 0}
    for option in options:
        if not isinstance(option, dict):
            continue
        parsed = _parse_matrix_occ(str(option.get("option") or ""))
        if parsed is None:
            continue
        root, expiration, call_put, strike = parsed
        if expiration < today:
            continue
        side = "call" if call_put == "C" else "put"
        totals[f"{side}_oi"] += max(0, int(option.get("open_interest") or 0))
        totals[f"{side}_vol"] += max(0, int(option.get("volume") or 0))
        if spot and abs(strike - spot) / spot > MATRIX_STRIKE_RANGE:
            continue
        open_interest = option.get("open_interest") or 0
        if open_interest <= 0:
            continue
        compact_options.append({
            "k": round(strike, 2),
            "t": call_put,
            "root": root,
            "exp": expiration.isoformat(),
            "dte": max(0, (expiration - today).days),
            "iv": round(option.get("iv") or 0, 4),
            "oi": int(open_interest),
            "vol": int(option.get("volume") or 0),
            "g": round(option.get("gamma") or 0, 7),
            "d": round(option.get("delta") or 0, 4),
        })
        carry = carries.get((root, expiration.isoformat()))
        if carry is not None:
            compact_options[-1]["b"] = round(carry, 5)
    return display_symbol, {
        "spot": round(spot, 2) if spot else None,
        "asof": data.get("last_trade_time") or today.isoformat(),
        "mult": 100,
        "opts": compact_options,
        "totals": totals,
    }


async def _fetch_matrix_cboe_symbol(
    client: httpx.AsyncClient,
    cboe_symbol: str,
    display_symbol: str,
    *,
    today: datetime.date,
) -> tuple[str, dict[str, Any]]:
    response = await client.get(
        MATRIX_OPTIONS_URL.format(cboe_symbol),
        headers={"User-Agent": "Tripity Matrix CBOE data proxy (+https://trytripity.site)"},
    )
    response.raise_for_status()
    return _compact_matrix_options_payload(display_symbol, response.json(), today=today)


async def _fetch_matrix_cboe_data_uncached() -> dict[str, Any]:
    today = datetime.date.today()
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        results = await asyncio.gather(
            *[
                _fetch_matrix_cboe_symbol(client, cboe_symbol, display_symbol, today=today)
                for cboe_symbol, display_symbol in MATRIX_CBOE_SYMBOLS
            ],
            return_exceptions=True,
        )
    out: dict[str, Any] = {}
    errors: list[str] = []
    for result in results:
        if isinstance(result, Exception):
            errors.append(str(result))
            continue
        symbol, data = result
        out[symbol] = data
    if not out:
        raise ValueError("; ".join(errors) or "CBOE returned no symbols")
    return out


async def _fetch_matrix_cboe_data() -> dict[str, Any]:
    global _matrix_cboe_data_cache
    now = time.time()
    if (
        _matrix_cboe_data_cache is not None
        and now - _matrix_cboe_data_cache[0] < MATRIX_CBOE_DATA_CACHE_SECONDS
    ):
        return _matrix_cboe_data_cache[1]
    async with _matrix_cboe_data_lock:
        now = time.time()
        if (
            _matrix_cboe_data_cache is not None
            and now - _matrix_cboe_data_cache[0] < MATRIX_CBOE_DATA_CACHE_SECONDS
        ):
            return _matrix_cboe_data_cache[1]
        data = await _fetch_matrix_cboe_data_uncached()
        _matrix_cboe_data_cache = (time.time(), data)
        return data


def _matrix_dynamic_symbol(value: str) -> str:
    symbol = value.upper().strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,5}", symbol):
        raise ValueError("Enter a valid US option symbol")
    return symbol


class MatrixDynamicRateLimitError(RuntimeError):
    pass


async def _fetch_matrix_dynamic_chain(symbol: str) -> dict[str, Any]:
    symbol = _matrix_dynamic_symbol(symbol)
    now = time.time()
    today_et = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    cached = _matrix_dynamic_chain_cache.get(symbol)
    if (
        cached is not None
        and datetime.datetime.fromtimestamp(cached[0], ZoneInfo("America/New_York")).date() == today_et
    ):
        return cached[1]
    async with _matrix_dynamic_chain_lock:
        now = time.time()
        cached = _matrix_dynamic_chain_cache.get(symbol)
        if (
            cached is not None
            and datetime.datetime.fromtimestamp(cached[0], ZoneInfo("America/New_York")).date() == today_et
        ):
            return cached[1]
        _matrix_dynamic_chain_fetches[:] = [
            fetched_at for fetched_at in _matrix_dynamic_chain_fetches if now - fetched_at < 60
        ]
        if len(_matrix_dynamic_chain_fetches) >= MATRIX_DYNAMIC_CHAIN_FETCHES_PER_MINUTE:
            raise MatrixDynamicRateLimitError("Too many new option symbols; try again in one minute")
        _matrix_dynamic_chain_fetches.append(now)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            _, record = await _fetch_matrix_cboe_symbol(
                client,
                symbol,
                symbol,
                today=datetime.date.today(),
            )
        if not record.get("spot") or not record.get("opts"):
            raise ValueError(f"CBOE returned no active option chain for {symbol}")
        record.update({
            "source": "cboe_daily_on_demand",
            "price_source": "Cboe delayed",
            "price_asof": record.get("asof"),
            "oi_source": "cboe_daily",
            "oi_asof": record.get("asof"),
            "dynamic": True,
            "history_available": False,
            "flow_available": False,
        })
        _matrix_dynamic_chain_cache[symbol] = (time.time(), record)
        if len(_matrix_dynamic_chain_cache) > MATRIX_DYNAMIC_CHAIN_CACHE_MAX:
            oldest = min(_matrix_dynamic_chain_cache, key=lambda key: _matrix_dynamic_chain_cache[key][0])
            _matrix_dynamic_chain_cache.pop(oldest, None)
        return record


def _remove_legacy_matrix_store() -> None:
    """Delete the old Matrix SQLite store (option flow, GEX replay snapshots,
    journal). Those screens are gone (ADR 0001), so the file and its data go
    with them. Safe to drop this function once production has restarted."""
    configured = os.getenv("TRIPITY_MATRIX_FLOW_DB", "").strip()
    data_dir = Path("/data")
    path = Path(configured) if configured else (data_dir if data_dir.exists() else Path.cwd()) / "matrix_flow.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            print(f"matrix: could not remove legacy store {candidate}: {exc}")


def _matrix_lse_api_key() -> str:
    return (
        os.getenv("TRIPITY_MATRIX_LSE_API_KEY")
        or os.getenv("LSE_API_KEY")
        or ""
    ).strip()


def _matrix_lse_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key, "User-Agent": "Tripity Matrix market data (+https://trytripity.site)"}


def _matrix_lse_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "results"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _matrix_lse_contract_key(row: dict[str, Any]) -> tuple[str, str, float] | None:
    ticker = str(row.get("ticker") or row.get("symbol") or "").removeprefix("O:")
    parsed = _parse_matrix_occ(ticker)
    if parsed is not None:
        _, expiration, call_put, strike = parsed
        return expiration.isoformat(), call_put, round(strike, 3)
    expiration = row.get("expiration") or row.get("expiry") or row.get("expiration_date")
    strike = row.get("strike")
    contract_type = str(row.get("contract_type") or row.get("type") or "").lower()
    call_put = "C" if contract_type in {"c", "call"} else "P" if contract_type in {"p", "put"} else ""
    if not expiration or strike is None or not call_put:
        return None
    return str(expiration)[:10], call_put, round(float(strike), 3)


def _merge_matrix_lse_chain(
    base: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = deepcopy(base)
    merged["oi_asof"] = base.get("oi_asof") or base.get("asof")
    merged["oi_source"] = "cboe_daily"
    lookup: dict[tuple[str, str, float], dict[str, Any]] = {}
    lse_asof = ""
    for row in rows:
        key = _matrix_lse_contract_key(row)
        if key is not None:
            lookup[key] = row
        timestamp = str(row.get("updated_at") or row.get("snapshot_time") or row.get("ts") or "")
        if timestamp > lse_asof:
            lse_asof = timestamp
    matches = 0
    for option in merged.get("opts") or []:
        key = (str(option.get("exp") or "")[:10], str(option.get("t") or ""), round(float(option.get("k") or 0), 3))
        live = lookup.get(key)
        if live is None:
            continue
        matches += 1
        # LSE v3 Greeks are computed twice a day (at least a session old), so
        # Cboe's IV/gamma/delta stay; only the live tape fields overlay.
        volume = live.get("volume_today", live.get("volume"))
        if volume is not None:
            option["vol"] = int(float(volume))
        last_price = live.get("last_price") or live.get("price")
        if last_price is not None:
            option["last"] = round(float(last_price), 6)
        bid = live.get("bid")
        ask = live.get("ask")
        if bid is not None:
            option["bid"] = round(float(bid), 6)
        if ask is not None:
            option["ask"] = round(float(ask), 6)
    merged["source"] = "lse_live+cboe_oi" if matches else "cboe_oi"
    merged["lse_rows"] = len(rows)
    merged["lse_matches"] = matches
    if lse_asof and matches:
        merged["asof"] = lse_asof
    return merged


async def _fetch_matrix_lse_chain(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    api_key: str,
    spot: float | None,
) -> tuple[str, list[dict[str, Any]]]:
    params: dict[str, str] = {"underlying": symbol, "limit": "5000"}
    if spot:
        params["strike_min"] = str(round(spot * (1 - MATRIX_STRIKE_RANGE), 4))
        params["strike_max"] = str(round(spot * (1 + MATRIX_STRIKE_RANGE), 4))
    for attempt in range(3):
        try:
            response = await client.get(
                MATRIX_LSE_CHAIN_URL,
                params=params,
                headers=_matrix_lse_headers(api_key),
            )
            response.raise_for_status()
            return symbol, _matrix_lse_rows(response.json())
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
    return symbol, []


async def _fetch_matrix_market_data_uncached() -> dict[str, Any]:
    base = deepcopy(await _fetch_matrix_cboe_data())
    for record in base.values():
        if isinstance(record, dict):
            record.setdefault("source", "tripity_cboe")
    api_key = _matrix_lse_api_key()
    if not api_key:
        return base
    timeout = httpx.Timeout(30.0, connect=10.0)
    results: list[tuple[str, list[dict[str, Any]]] | Exception] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        # LSE chain responses are large. Fetching every symbol concurrently can
        # trip transient upstream limits and leave individual symbols on the
        # fallback feed, so keep this deliberately paced.
        for symbol in MATRIX_LSE_SYMBOLS:
            if symbol not in base:
                continue
            try:
                result = await _fetch_matrix_lse_chain(
                    client,
                    symbol,
                    api_key=api_key,
                    spot=float(base.get(symbol, {}).get("spot") or 0) or None,
                )
            except Exception as exc:
                results.append(exc)
            else:
                results.append(result)
    for result in results:
        if isinstance(result, Exception):
            continue
        symbol, rows = result
        base[symbol] = _merge_matrix_lse_chain(base[symbol], rows)
    return base


async def _fetch_matrix_market_data_cached() -> dict[str, Any]:
    global _matrix_market_data_cache
    now = time.time()
    if (
        _matrix_market_data_cache is not None
        and now - _matrix_market_data_cache[0] < MATRIX_MARKET_DATA_CACHE_SECONDS
    ):
        return _matrix_market_data_cache[1]
    async with _matrix_market_data_lock:
        now = time.time()
        if (
            _matrix_market_data_cache is not None
            and now - _matrix_market_data_cache[0] < MATRIX_MARKET_DATA_CACHE_SECONDS
        ):
            return _matrix_market_data_cache[1]
        data = await _fetch_matrix_market_data_uncached()
        _matrix_market_data_cache = (time.time(), data)
        return data


async def _fetch_matrix_lse_spots() -> dict[str, dict[str, Any]]:
    global _matrix_lse_spots_cache
    api_key = _matrix_lse_api_key()
    if not api_key:
        return {}
    now = time.time()
    if _matrix_lse_spots_cache is not None and now - _matrix_lse_spots_cache[0] < MATRIX_CANDLES_CACHE_SECONDS:
        return _matrix_lse_spots_cache[1]
    async with _matrix_lse_spots_lock:
        now = time.time()
        if _matrix_lse_spots_cache is not None and now - _matrix_lse_spots_cache[0] < MATRIX_CANDLES_CACHE_SECONDS:
            return _matrix_lse_spots_cache[1]
        semaphore = asyncio.Semaphore(2)

        async def fetch_one(client: httpx.AsyncClient, display_symbol: str, lse_pair: tuple[str, str]):
            market, lse_symbol = lse_pair
            async with semaphore:
                # Without from/to, preview answers the newest ten 1m candles, oldest first.
                response = await client.get(
                    MATRIX_LSE_PREVIEW_URL,
                    params={"market": market, "symbol": lse_symbol, "timeframe": "1m", "data": "candles"},
                    headers=_matrix_lse_headers(api_key),
                )
                response.raise_for_status()
            rows = _matrix_lse_rows(response.json())
            if not rows:
                raise ValueError(f"LSE returned no price for {display_symbol}")
            row = rows[-1]
            asof = str(row.get("time") or row.get("ts") or row.get("timestamp") or "")
            if asof and not re.search(r"Z$|[+-]\d{2}:?\d{2}$", asof):
                asof = asof.replace(" ", "T") + "Z"
            # A stale LSE print must not override the Cboe spot (see the candle guard).
            seen = datetime.datetime.fromisoformat(asof.replace("Z", "+00:00")).timestamp() if asof else 0.0
            if seen < time.time() - MATRIX_LSE_CANDLE_MAX_AGE_SECONDS:
                raise ValueError(f"LSE price for {display_symbol} is stale ({asof or 'no time'})")
            return display_symbol, {
                "spot": float(row.get("close") or row.get("price")),
                "asof": asof,
            }

        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            results = await asyncio.gather(
                *(fetch_one(client, display, upstream) for display, upstream in MATRIX_LSE_PRICE_SYMBOLS.items()),
                return_exceptions=True,
            )
        spots = {result[0]: result[1] for result in results if not isinstance(result, Exception)}
        _matrix_lse_spots_cache = (time.time(), spots)
        return spots


async def _fetch_matrix_market_data() -> dict[str, Any]:
    # The chains and the live spots come from different calls, so fetch them together.
    market, spots = await asyncio.gather(_fetch_matrix_market_data_cached(), _fetch_matrix_lse_spots())
    data = deepcopy(market)
    for symbol, live in spots.items():
        record = data.get(symbol)
        if not isinstance(record, dict):
            continue
        record["oi_asof"] = record.get("oi_asof") or record.get("asof")
        record["oi_source"] = "cboe_daily"
        record["spot"] = live["spot"]
        record["asof"] = live["asof"]
        record["price_asof"] = live["asof"]
        record["price_source"] = "lse_v3:" + "/".join(MATRIX_LSE_PRICE_SYMBOLS[symbol])
        if symbol in {"SPX", "NDX"}:
            record["source"] = "lse_live_index+cboe_oi"
        elif record.get("source") == "tripity_cboe":
            record["source"] = "lse_live+cboe_oi"
    return data


def _accepts_gzip(scope: dict[str, Any]) -> bool:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"accept-encoding" and b"gzip" in value.lower():
            return True
    return False


class PublicCompanyApp:
    """ASGI wrapper with a create page/API and dynamic /mcp/{slug} routing."""

    def __init__(
        self,
        connector: PublicConnector,
        *,
        oauth: bool,
        public_url: str,
        storage_file: str | Path | None = None,
        oauth_provider: Any = None,
        spec_loader: Any = None,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.connectors: dict[str, PublicConnector] = {connector.slug: connector}
        self.default_slug = connector.slug
        self.slug = connector.slug
        self.mcp_path = connector.mcp_path
        self.enabled_tools = connector.enabled_tools
        self.disabled_tools = connector.disabled_tools
        self.oauth = oauth
        self.public_url = public_url.rstrip("/")
        self.storage_file = Path(storage_file) if storage_file else None
        self.assisted_requests_file = (self.storage_file.parent / "assisted_setup_requests.jsonl") if self.storage_file else None
        self.oauth_provider = oauth_provider
        self.spec_loader = spec_loader
        self.upstream_transport = upstream_transport
        self._lifespans: dict[str, Any] = {}
        self._stack: AsyncExitStack | None = None
        self._started = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if scope["type"] != "http":
            await self.connectors[self.default_slug].app(scope, receive, send)
            return
        path = scope.get("path", "/")
        method = scope.get("method", "GET")
        if method == "GET" and path == "/":
            # The site is the Matrix dashboard (matrixgex.app): open it at the root.
            await self._response(
                send,
                307,
                b"",
                b"text/plain; charset=utf-8",
                headers=[(b"location", b"/matrix/")],
            )
            return
        if method == "GET" and path == "/health":
            await self._json(send, {"status": "ok", "mcp_path": self.mcp_path, "connectors": sorted(self.connectors), "create_path": "/create", "auth": "oauth" if self.oauth else "bearer"})
            return
        if method == "GET" and path == "/create":
            await self._response(send, 200, CREATE_PAGE.encode("utf-8"), b"text/html; charset=utf-8")
            return
        if method == "GET" and path == "/matrix":
            query = scope.get("query_string", b"")
            await self._response(
                send,
                307,
                b"",
                b"text/plain; charset=utf-8",
                headers=[(b"location", b"/matrix/" + (b"?" + query if query else b""))],
            )
            return
        matrix_static = {
            "/matrix/matrix.css": ("matrix.css", b"text/css; charset=utf-8"),
            "/matrix/matrix.js": ("matrix.js", b"text/javascript; charset=utf-8"),
        }
        if path.rstrip("/") in MATRIX_SCREEN_PATHS:
            matrix_file = ("matrix.html", b"text/html; charset=utf-8")
        else:
            matrix_file = matrix_static.get(path)
        if method == "GET" and matrix_file:
            filename, content_type = matrix_file
            await self._response(
                send,
                200,
                (MATRIX_WEB_DIR / filename).read_bytes(),
                content_type,
                headers=[(b"cache-control", b"public, max-age=60")],
            )
            return
        if method == "GET" and path == "/api/public-connectors":
            await self._json(send, {"items": [self._connector_dict(c) for c in self.connectors.values()]})
            return
        if method == "POST" and path == "/api/public-connectors":
            await self._create_connector(receive, send)
            return
        if method == "POST" and path == "/api/analyze-source":
            await self._analyze_source(receive, send)
            return
        if method == "POST" and path == "/api/assisted-setup-requests":
            await self._create_assisted_request(receive, send)
            return
        if method == "GET" and path == "/api/assisted-setup-requests":
            await self._list_assisted_requests(send)
            return
        if path == "/api/matrix/spx-quote":
            if method == "OPTIONS":
                await self._response(
                    send,
                    204,
                    b"",
                    b"text/plain; charset=utf-8",
                    headers=self._matrix_cors_headers(scope),
                )
                return
            if method == "GET":
                await self._matrix_spx_quote(scope, send)
                return
        if path == "/api/matrix/cboe-data":
            if method == "OPTIONS":
                await self._response(
                    send,
                    204,
                    b"",
                    b"text/plain; charset=utf-8",
                    headers=self._matrix_cors_headers(scope),
                )
                return
            if method == "GET":
                await self._matrix_cboe_data(scope, send)
                return
        if path == "/api/matrix/symbol-data":
            if method == "OPTIONS":
                await self._response(
                    send,
                    204,
                    b"",
                    b"text/plain; charset=utf-8",
                    headers=self._matrix_cors_headers(scope),
                )
                return
            if method == "GET":
                await self._matrix_symbol_data(scope, send)
                return
        if path == "/api/matrix/candles":
            if method == "OPTIONS":
                await self._response(
                    send,
                    204,
                    b"",
                    b"text/plain; charset=utf-8",
                    headers=self._matrix_cors_headers(scope),
                )
                return
            if method == "GET":
                await self._matrix_candles(scope, send)
                return

        target = self._target_connector(path)
        if target is not None:
            await target.app(scope, receive, send)
            return
        # OAuth server routes are host-global; any connector app can serve them because
        # all dynamic apps share the same provider instance.
        if path in {"/.well-known/oauth-authorization-server", "/authorize", "/token", "/register"}:
            await self.connectors[self.default_slug].app(scope, receive, send)
            return
        await self._response(send, 404, b"Not Found", b"text/plain; charset=utf-8")

    def _target_connector(self, path: str) -> PublicConnector | None:
        if path.startswith("/mcp/"):
            slug = path.split("/", 3)[2]
            return self.connectors.get(slug)
        prefix = "/.well-known/oauth-protected-resource/mcp/"
        if path.startswith(prefix):
            slug = path[len(prefix):].split("/", 1)[0]
            return self.connectors.get(slug)
        return None

    async def _handle_lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    self._stack = AsyncExitStack()
                    for connector in list(self.connectors.values()):
                        await self._start_connector_lifespan(connector)
                    await asyncio.to_thread(_remove_legacy_matrix_store)
                    self._started = True
                    await send({"type": "lifespan.startup.complete"})
                except BaseException as exc:  # noqa: BLE001
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
            elif message["type"] == "lifespan.shutdown":
                if self._stack is not None:
                    await self._stack.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _start_connector_lifespan(self, connector: PublicConnector) -> None:
        if self._stack is None or connector.slug in self._lifespans:
            return
        router = getattr(connector.app, "router", None)
        context_factory = getattr(router, "lifespan_context", None)
        if context_factory is not None:
            ctx = context_factory(connector.app)
            await self._stack.enter_async_context(ctx)
            self._lifespans[connector.slug] = ctx

    async def _create_connector(self, receive: Any, send: Any) -> None:
        try:
            body = await self._read_body(receive)
            payload = json.loads(body.decode("utf-8") or "{}")
            company_name = str(payload.get("company_name") or "").strip()
            openapi_url = str(payload.get("openapi_url") or "").strip()
            api_base_url = str(payload.get("api_base_url") or "").strip() or None
            if not company_name:
                raise ValueError("company_name is required")
            if isinstance(payload.get("har"), dict):
                draft_spec = har_to_openapi(payload["har"], title=f"{company_name} HAR Draft API")
                openapi_url = "har://approved-upload"
                api_base_url = api_base_url or (draft_spec.get("servers") or [{}])[0].get("url")
                connector = _build_public_connector_from_spec(
                    company_name=company_name,
                    openapi_url=openapi_url,
                    openapi_spec=draft_spec,
                    api_base_url=api_base_url,
                    public_url=self.public_url,
                    oauth=self.oauth,
                    oauth_provider=self.oauth_provider,
                    upstream_transport=self.upstream_transport,
                )
            else:
                if not openapi_url:
                    raise ValueError("openapi_url or har is required")
                connector = _build_public_connector(
                    company_name=company_name,
                    openapi_url=openapi_url,
                    api_base_url=api_base_url,
                    public_url=self.public_url,
                    oauth=self.oauth,
                    oauth_provider=self.oauth_provider,
                    spec_loader=self.spec_loader,
                    upstream_transport=self.upstream_transport,
                )
            self.connectors[connector.slug] = connector
            if self._started:
                await self._start_connector_lifespan(connector)
            self._save_connectors()
            await self._json(send, self._connector_dict(connector), status=201)
        except Exception as exc:  # noqa: BLE001
            await self._json(send, {"detail": str(exc)}, status=400)

    async def _analyze_source(self, receive: Any, send: Any) -> None:
        try:
            body = await self._read_body(receive)
            payload = json.loads(body.decode("utf-8") or "{}")
            url = str(payload.get("url") or "").strip()
            parsed = urlsplit(url)
            own = url.startswith(self.public_url + "/")
            if own and self._target_connector(parsed.path) is not None:
                await self._json(send, {
                    "kind": "existing_mcp",
                    "url": url,
                    "message": "This is already a Tripity MCP connector.",
                    "mcp_url": url,
                    "openapi_url": None,
                    "auth": "oauth" if self.oauth else "bearer",
                    "evidence": ["matched local Tripity connector route"],
                })
                return
            await self._json(send, analyze_source_url(url).to_dict())
        except Exception as exc:  # noqa: BLE001
            await self._json(send, {"kind": "invalid", "detail": str(exc)}, status=400)

    async def _create_assisted_request(self, receive: Any, send: Any) -> None:
        try:
            body = await self._read_body(receive)
            payload = json.loads(body.decode("utf-8") or "{}")
            email = str(payload.get("email") or "").strip()
            app_url = str(payload.get("app_url") or "").strip()
            questions = str(payload.get("questions") or "").strip()
            if not email or "@" not in email:
                raise ValueError("A valid email is required")
            if not app_url:
                raise ValueError("App/API/docs URL is required")
            if not questions:
                raise ValueError("Tell us what AI should answer or do")
            record = {
                "id": f"lead-{int(time.time())}",
                "created_at": int(time.time()),
                "email": email,
                "app_url": app_url,
                "questions": questions,
                "status": "new",
            }
            if self.assisted_requests_file is not None:
                self.assisted_requests_file.parent.mkdir(parents=True, exist_ok=True)
                with self.assisted_requests_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, sort_keys=True) + "\n")
            await self._json(send, {"ok": True, "request": record}, status=201)
        except Exception as exc:  # noqa: BLE001
            await self._json(send, {"detail": str(exc)}, status=400)

    async def _list_assisted_requests(self, send: Any) -> None:
        # For the demo this is open; production should put this behind operator auth.
        items: list[dict[str, Any]] = []
        if self.assisted_requests_file is not None and self.assisted_requests_file.exists():
            for line in self.assisted_requests_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        await self._json(send, {"items": items})

    async def _matrix_spx_quote(self, scope: dict[str, Any], send: Any) -> None:
        try:
            quote = await _fetch_matrix_spx_quote()
            await self._json(
                send,
                quote,
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"public, max-age=20"),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_cboe_data(self, scope: dict[str, Any], send: Any) -> None:
        try:
            data = await _fetch_matrix_market_data()
            body = json.dumps(data, separators=(",", ":")).encode("utf-8")
            headers = [
                *self._matrix_cors_headers(scope),
                (b"cache-control", b"public, max-age=30"),
                (b"vary", b"accept-encoding"),
            ]
            # About 4 MB of JSON shrinks to about 0.5 MB, which matters on a phone.
            if _accepts_gzip(scope):
                body = gzip.compress(body, compresslevel=5)
                headers.append((b"content-encoding", b"gzip"))
            await self._response(send, 200, body, b"application/json", headers=headers)
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_symbol_data(self, scope: dict[str, Any], send: Any) -> None:
        try:
            query = parse_qs(scope.get("query_string", b"").decode("latin1"))
            symbol = _matrix_dynamic_symbol((query.get("symbol") or [""])[0])
            record = await _fetch_matrix_dynamic_chain(symbol)
            await self._json(
                send,
                {
                    "ok": True,
                    "symbol": symbol,
                    "record": record,
                    "cache_scope": "et_calendar_day",
                    "spot_source": "cboe_delayed",
                },
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"public, max-age=300"),
                ],
            )
        except ValueError as exc:
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=400,
                headers=self._matrix_cors_headers(scope),
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_candles(self, scope: dict[str, Any], send: Any) -> None:
        try:
            query = parse_qs(scope.get("query_string", b"").decode("latin1"))
            data = await _fetch_matrix_candles(
                symbol=(query.get("symbol") or ["SPY"])[0],
                interval=(query.get("interval") or ["1m"])[0],
                range_=(query.get("range") or ["1d"])[0],
            )
            await self._json(
                send,
                data,
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"public, max-age=15"),
                ],
            )
        except ValueError as exc:
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=400,
                headers=self._matrix_cors_headers(scope),
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    def _connector_dict(self, connector: PublicConnector) -> dict[str, Any]:
        return {
            "slug": connector.slug,
            "company_name": connector.company_name,
            "openapi_url": connector.openapi_url,
            "api_base_url": connector.api_base_url,
            "mcp_url": f"{self.public_url}{connector.mcp_path}",
            "mcp_path": connector.mcp_path,
            "enabled_tools": list(connector.enabled_tools),
            "disabled_tools": list(connector.disabled_tools),
            "auth": "oauth" if self.oauth else "bearer",
        }

    async def _read_body(self, receive: Any) -> bytes:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                return b"".join(chunks)

    def _matrix_cors_headers(self, scope: dict[str, Any]) -> list[tuple[bytes, bytes]]:
        origin = ""
        for key, value in scope.get("headers", []):
            if key.lower() == b"origin":
                origin = value.decode("latin1")
                break
        allowed = {
            item.strip()
            for item in os.getenv(
                "TRIPITY_MATRIX_CORS_ORIGINS",
                ",".join(sorted(MATRIX_CORS_ORIGINS)),
            ).split(",")
            if item.strip()
        }
        headers = [
            (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
            (b"access-control-allow-headers", b"content-type"),
        ]
        if origin in allowed:
            headers.append((b"access-control-allow-origin", origin.encode("latin1")))
        return headers

    async def _json(
        self,
        send: Any,
        payload: dict[str, Any],
        *,
        status: int = 200,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        await self._response(
            send,
            status,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            b"application/json",
            headers=headers,
        )

    async def _response(
        self,
        send: Any,
        status: int,
        body: bytes,
        content_type: bytes,
        *,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type), *(headers or [])],
        })
        await send({"type": "http.response.body", "body": body})

    def _save_connectors(self) -> None:
        if self.storage_file is None:
            return
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"company_name": c.company_name, "openapi_url": c.openapi_url, "api_base_url": c.api_base_url, "openapi_spec": c.openapi_spec if c.openapi_url.startswith("har://") else None}
            for c in self.connectors.values()
        ]
        self.storage_file.write_text(json.dumps(records, sort_keys=True), encoding="utf-8")

    def restore_saved(self) -> None:
        if self.storage_file is None or not self.storage_file.exists():
            return
        try:
            records = json.loads(self.storage_file.read_text(encoding="utf-8"))
        except Exception:
            return
        for record in records:
            try:
                if isinstance(record.get("openapi_spec"), dict):
                    connector = _build_public_connector_from_spec(
                        company_name=record["company_name"],
                        openapi_url=record["openapi_url"],
                        openapi_spec=record["openapi_spec"],
                        api_base_url=record.get("api_base_url"),
                        public_url=self.public_url,
                        oauth=self.oauth,
                        oauth_provider=self.oauth_provider,
                        upstream_transport=self.upstream_transport,
                    )
                else:
                    connector = _build_public_connector(
                        company_name=record["company_name"],
                        openapi_url=record["openapi_url"],
                        api_base_url=record.get("api_base_url"),
                        public_url=self.public_url,
                        oauth=self.oauth,
                        oauth_provider=self.oauth_provider,
                        spec_loader=self.spec_loader,
                        upstream_transport=self.upstream_transport,
                    )
                self.connectors[connector.slug] = connector
            except Exception:
                continue


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _validate_public_openapi_url(openapi_url: str) -> None:
    parsed = urlsplit(openapi_url)
    if parsed.scheme not in {"http", "https"}:
        raise IntakeError("Only HTTP and HTTPS URLs are allowed")
    if not parsed.hostname:
        raise IntakeError("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise IntakeError("Credentials are not allowed in OpenAPI URLs")
    try:
        records = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise IntakeError("URL hostname could not be resolved") from exc
    addresses = sorted({record[4][0] for record in records})
    if not addresses:
        raise IntakeError("URL hostname resolved to no addresses")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise IntakeError("URL hostname resolved to an invalid address") from exc
        if not parsed_address.is_global:
            raise IntakeError("URL hostname resolves to a private or reserved address")


OPENAPI_DISCOVERY_PATHS = (
    "/openapi.json",
    "/openapi.yaml",
    "/openapi.yml",
    "/swagger.json",
    "/swagger.yaml",
    "/swagger.yml",
    "/api/openapi.json",
    "/api/openapi.yaml",
    "/api/swagger.json",
    "/api-docs",
    "/api/docs",
    "/docs/openapi.json",
    "/v3/api-docs",
    "/api/v3/openapi.json",
)


def _load_spec(openapi_url: str) -> dict[str, Any]:
    _validate_public_openapi_url(openapi_url)
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        with client.stream("GET", openapi_url, headers={"Accept": "application/json, application/yaml, text/yaml"}) as response:
            if response.is_redirect:
                raise IntakeError("Redirects are not followed; provide the final OpenAPI URL")
            response.raise_for_status()
            spec = parse_openapi_text(response.read())
    if not isinstance(spec, dict) or not (spec.get("openapi") or spec.get("swagger")):
        raise IntakeError("OpenAPI document must be an object")
    return spec


def _candidate_openapi_urls(input_url: str) -> tuple[str, ...]:
    parsed = urlsplit(input_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return (input_url,)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path_hint = parsed.path.lower()
    looks_like_spec = any(part in path_hint for part in ("openapi", "swagger", "api-docs")) or path_hint.endswith((".json", ".yaml", ".yml"))
    candidates = [input_url] if looks_like_spec else []
    for path in OPENAPI_DISCOVERY_PATHS:
        candidates.append(urljoin(origin, path))
    # Also try relative to the provided docs/API path.
    base = input_url if input_url.endswith("/") else input_url.rsplit("/", 1)[0] + "/"
    for name in ("openapi.json", "openapi.yaml", "swagger.json", "swagger.yaml"):
        candidates.append(urljoin(base, name))
    if not looks_like_spec:
        candidates.append(input_url)
    return tuple(dict.fromkeys(candidates))


def _load_spec_with_discovery(input_url: str) -> tuple[dict[str, Any], str]:
    errors: list[str] = []
    for candidate in _candidate_openapi_urls(input_url):
        try:
            return _load_spec(candidate), candidate
        except Exception as exc:  # noqa: BLE001 - keep trying common discovery paths
            errors.append(f"{candidate}: {exc}")
    raise IntakeError("Could not find a valid OpenAPI document. Tried common locations. Provide an OpenAPI URL or request assisted setup.")


def _build_oauth_provider(public_url: str) -> Any:
    from mcp.server.auth.settings import ClientRegistrationOptions

    state_file = os.getenv("TRIPITY_OAUTH_STATE_FILE")
    if state_file:
        from tripity_experiment.oauth_state import PersistentOAuthProvider

        key = os.getenv("TRIPITY_ENCRYPTION_KEY")
        if not key:
            raise RuntimeError("TRIPITY_ENCRYPTION_KEY is required with TRIPITY_OAUTH_STATE_FILE")
        return PersistentOAuthProvider(
            state_path=state_file,
            encryption_key=key,
            base_url=public_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
    from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

    return InMemoryOAuthProvider(base_url=public_url, client_registration_options=ClientRegistrationOptions(enabled=True))


def _synthesize_operation_ids(openapi_spec: dict[str, Any]) -> dict[str, Any]:
    spec = deepcopy(openapi_spec)
    seen: set[str] = set()
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"} or not isinstance(operation, dict):
                continue
            existing = operation.get("operationId")
            if isinstance(existing, str) and existing and existing not in seen:
                seen.add(existing)
                continue
            parts = [p for p in str(path).strip("/").split("/") if p]
            normalized = "_".join(re.sub(r"[^a-zA-Z0-9_]+", "_", p.strip("{}")) for p in parts) or "root"
            candidate = f"{method.lower()}_{normalized}"[:80]
            candidate = re.sub(r"_+", "_", candidate).strip("_") or f"{method.lower()}_root"
            base = candidate
            suffix = 2
            while candidate in seen:
                candidate = f"{base}_{suffix}"
                suffix += 1
            operation["operationId"] = candidate
            seen.add(candidate)
    return spec


def _build_public_connector_from_spec(
    *,
    company_name: str,
    openapi_url: str,
    openapi_spec: dict[str, Any],
    api_base_url: str | None,
    public_url: str,
    oauth: bool,
    oauth_provider: Any = None,
    client_token: str | None = None,
    upstream_bearer_token: str | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> PublicConnector:
    openapi_spec = _synthesize_operation_ids(openapi_spec)
    api_base_url = api_base_url or infer_api_base_url(openapi_spec, openapi_url if openapi_url.startswith(("http://", "https://")) else api_base_url or "https://example.invalid/openapi.json")
    draft = build_company_connector_draft(
        company_name=company_name,
        openapi_spec=openapi_spec,
        api_base_url=api_base_url,
        public_mcp_base_url=public_url,
        allow_write_operation_ids=set(),
    )
    manifest = manifest_from_draft(draft)
    enabled = list(manifest.enabled_tool_names())
    if not enabled:
        raise RuntimeError("Company connector has no read tools to serve")
    curated_spec = apply_tool_curation_to_spec(openapi_spec, manifest.tools)
    mcp = build_mcp_server(
        openapi_spec=curated_spec,
        base_url=api_base_url,
        allowed_operation_ids=enabled,
        bearer_token=upstream_bearer_token,
        transport=upstream_transport,
        auth=oauth_provider if oauth else None,
    )
    mcp_path = f"/mcp/{draft.slug}"
    app: Any = mcp.http_app(path=mcp_path, stateless_http=True)
    if client_token and not oauth:
        app = BearerGate(app, client_token)
    return PublicConnector(
        slug=draft.slug,
        company_name=company_name,
        openapi_url=openapi_url,
        api_base_url=api_base_url,
        mcp_path=mcp_path,
        enabled_tools=tuple(enabled),
        disabled_tools=manifest.disabled_tool_names(),
        app=app,
        openapi_spec=openapi_spec,
    )


def _build_public_connector(
    *,
    company_name: str,
    openapi_url: str,
    api_base_url: str | None,
    public_url: str,
    oauth: bool,
    oauth_provider: Any = None,
    client_token: str | None = None,
    upstream_bearer_token: str | None = None,
    spec_loader: Any = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> PublicConnector:
    if spec_loader is None:
        spec, openapi_url = _load_spec_with_discovery(openapi_url)
    else:
        spec = spec_loader(openapi_url)
    return _build_public_connector_from_spec(
        company_name=company_name,
        openapi_url=openapi_url,
        openapi_spec=spec,
        api_base_url=api_base_url,
        public_url=public_url,
        oauth=oauth,
        oauth_provider=oauth_provider,
        client_token=client_token,
        upstream_bearer_token=upstream_bearer_token,
        upstream_transport=upstream_transport,
    )


def create_public_company_app(
    *,
    company_name: str | None = None,
    openapi_url: str | None = None,
    api_base_url: str | None = None,
    upstream_bearer_token: str | None = None,
    oauth: bool | None = None,
    client_token: str | None = None,
    public_url: str | None = None,
    spec_loader: Any = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> PublicCompanyApp:
    company_name = company_name or os.getenv("TRIPITY_COMPANY_NAME", DEFAULT_COMPANY_NAME)
    openapi_url = openapi_url or os.getenv("TRIPITY_OPENAPI_URL", DEFAULT_OPENAPI_URL)
    public_url = (public_url or os.getenv("TRIPITY_PUBLIC_URL", "http://localhost:8080")).rstrip("/")
    oauth = _env_flag("TRIPITY_OAUTH_ENABLED") if oauth is None else oauth
    client_token = client_token or os.getenv("TRIPITY_CLIENT_BEARER_TOKEN")
    upstream_bearer_token = upstream_bearer_token or os.getenv("TRIPITY_UPSTREAM_BEARER_TOKEN")
    oauth_provider = _build_oauth_provider(public_url) if oauth else None
    connector = _build_public_connector(
        company_name=company_name,
        openapi_url=openapi_url,
        api_base_url=api_base_url or os.getenv("TRIPITY_API_BASE_URL"),
        public_url=public_url,
        oauth=oauth,
        oauth_provider=oauth_provider,
        client_token=client_token,
        upstream_bearer_token=upstream_bearer_token,
        spec_loader=spec_loader,
        upstream_transport=upstream_transport,
    )
    storage_file = os.getenv("TRIPITY_PUBLIC_CONNECTORS_FILE")
    app = PublicCompanyApp(
        connector,
        oauth=oauth,
        public_url=public_url,
        storage_file=storage_file,
        oauth_provider=oauth_provider,
        spec_loader=spec_loader,
        upstream_transport=upstream_transport,
    )
    app.restore_saved()
    return app


def main() -> None:
    uvicorn.run(
        "tripity_experiment.public_company_host:create_public_company_app",
        factory=True,
        host=os.getenv("TRIPITY_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("TRIPITY_PORT", "8080"))),
        log_level=os.getenv("TRIPITY_LOG_LEVEL", "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
