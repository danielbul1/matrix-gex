"""Public host for Tripity company connectors: create page + /mcp/{company} + OAuth."""

from __future__ import annotations

import html
import ipaddress
import asyncio
import csv
import datetime
import io
import json
import os
import re
import socket
import sqlite3
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

from tripity_experiment import matrix_gex
from tripity_experiment import matrix_outcome
from tripity_experiment import matrix_regime
from tripity_experiment.company_api import infer_api_base_url
from tripity_experiment.har_openapi import har_to_openapi
from tripity_experiment.company_connector import build_company_connector_draft
from tripity_experiment.connector_manifest import manifest_from_draft
from tripity_experiment.mcp_adapter import build_mcp_server
from tripity_experiment.openapi_intake import IntakeError, parse_openapi_text
from tripity_experiment.project_gateway import BearerGate
from tripity_experiment.semantic_curation import apply_tool_curation_to_spec
from tripity_experiment.source_analysis import analyze_source_url

DEFAULT_OPENAPI_URL = "https://petstore3.swagger.io/api/v3/openapi.json"
DEFAULT_COMPANY_NAME = "Swagger Petstore"
MATRIX_SPX_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_SPX.json"
MATRIX_VIX_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
MATRIX_OPTIONS_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
MATRIX_CBOE_SYMBOL_DIRECTORY_URL = (
    "https://www.cboe.com/us/options/symboldir/equity_index_options/?download=csv"
)
MATRIX_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
MATRIX_LSE_CHAIN_URL = "https://api.londonstrategicedge.com/vault/options/chain"
MATRIX_LSE_CANDLES_URL = "https://api.londonstrategicedge.com/vault/candles"
MATRIX_LSE_FLOW_URL = "https://api.londonstrategicedge.com/vault/options/flow"
MATRIX_LSE_SYMBOLS = ("SPY", "QQQ")
# Index underlyings are streamed under their LSE catalog names (same names the
# REST vault accepts). If LSE ever rejects these on the free plan, set
# TRIPITY_MATRIX_STREAM_INDICES=0 to fall back to REST-only index spots.
MATRIX_LSE_INDEX_STREAM_SYMBOLS = {"SPX500/USD": "SPX", "NAS100/USD": "NDX"}
MATRIX_LSE_PRICE_SYMBOLS = {
    "SPX": "SPX500/USD",
    "NDX": "NAS100/USD",
    "SPY": "SPY",
    "QQQ": "QQQ",
}
MATRIX_CANDLE_SYMBOLS = set(MATRIX_LSE_PRICE_SYMBOLS)
MATRIX_YAHOO_SYMBOLS = {"SPX": "^GSPC", "NDX": "^NDX"}
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
MATRIX_SYMBOL_CATALOG_CACHE_SECONDS = 86400.0
MATRIX_DYNAMIC_CHAIN_CACHE_MAX = 64
MATRIX_DYNAMIC_CHAIN_FETCHES_PER_MINUTE = 12
MATRIX_MARKET_DATA_CACHE_SECONDS = 300.0
MATRIX_CANDLES_CACHE_SECONDS = 50.0
# REST flow is a bootstrap/backfill source. Live charts read the persistent
# WebSocket minute store, so refreshing a browser must not poll 5,000 rows.
MATRIX_FLOW_CACHE_SECONDS = 900.0
MATRIX_FLOW_INTERVALS = {"1m": 60, "5m": 300, "15m": 900}
MATRIX_GEX_SNAPSHOT_INTERVAL_SECONDS = 60
MATRIX_SPOTS_CACHE_CONTROL = b"public, max-age=2"
_MATRIX_PACKAGED_WEB_DIR = Path(__file__).with_name("web")
MATRIX_WEB_DIR = (
    _MATRIX_PACKAGED_WEB_DIR
    if (_MATRIX_PACKAGED_WEB_DIR / "matrix.html").exists()
    else Path.cwd() / "src" / "tripity_experiment" / "web"
)
_matrix_cboe_data_cache: tuple[float, dict[str, Any]] | None = None
_matrix_cboe_data_lock = asyncio.Lock()
_matrix_dynamic_chain_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_matrix_dynamic_chain_lock = asyncio.Lock()
_matrix_dynamic_chain_fetches: list[float] = []
_matrix_symbol_catalog_cache: tuple[float, list[dict[str, str]]] | None = None
_matrix_symbol_catalog_lock = asyncio.Lock()
_matrix_market_data_cache: tuple[float, dict[str, Any]] | None = None
_matrix_market_data_lock = asyncio.Lock()
_matrix_candles_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_matrix_candles_lock = asyncio.Lock()
_matrix_flow_cache: dict[tuple[str, str, tuple[str, ...]], tuple[float, dict[str, Any]]] = {}
_matrix_flow_lock = asyncio.Lock()
_matrix_flow_collector: MatrixFlowCollector | None = None
_matrix_lse_spots_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
_matrix_lse_spots_lock = asyncio.Lock()
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
    async with httpx.AsyncClient(timeout=10.0) as client:
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


# VIX has no LSE websocket stream, so its spot rides the CBOE delayed quote
# (same fallback pattern as the SPX quote), cached briefly for the poller.
MATRIX_CBOE_SPOT_URLS = {"VIX": MATRIX_VIX_QUOTE_URL}
_matrix_cboe_spot_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _fetch_matrix_cboe_spot_cached(symbol: str) -> dict[str, Any] | None:
    url = MATRIX_CBOE_SPOT_URLS.get(symbol)
    if not url:
        return None
    now = time.time()
    cached = _matrix_cboe_spot_cache.get(symbol)
    if cached and now - cached[0] < MATRIX_CANDLES_CACHE_SECONDS:
        return cached[1]
    try:
        quote = await _fetch_matrix_cboe_quote(url, symbol, f"_{symbol}")
    except Exception:  # noqa: BLE001
        return cached[1] if cached else None
    entry = {"spot": quote["spot"], "asof": quote.get("asof"), "source": "cboe_delayed_quote"}
    _matrix_cboe_spot_cache[symbol] = (time.time(), entry)
    return entry


def _matrix_lse_candles_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in rows:
        timestamp = row.get("ts") or row.get("timestamp")
        if not timestamp:
            continue
        try:
            parsed = datetime.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
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
    symbol = symbol.upper()
    if symbol not in MATRIX_CANDLE_SYMBOLS:
        raise ValueError("Unsupported candle symbol")
    if interval not in MATRIX_CANDLE_INTERVALS:
        raise ValueError("Unsupported candle interval")
    api_key = _matrix_lse_api_key()
    lse_symbol = MATRIX_LSE_PRICE_SYMBOLS.get(symbol)
    if api_key and lse_symbol:
        params = {
            "symbol": lse_symbol,
            "timeframe": interval,
            "order": "desc",
            "limit": "2500" if range_ != "1d" else "500",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    MATRIX_LSE_CANDLES_URL,
                    params=params,
                    headers={
                        "x-api-key": api_key,
                        "User-Agent": "Tripity Matrix market data (+https://trytripity.site)",
                    },
                )
                response.raise_for_status()
            candles = _matrix_lse_candles_payload(_matrix_lse_rows(response.json()))
            if candles:
                return {
                    "ok": True,
                    "symbol": symbol,
                    "interval": interval,
                    "range": range_,
                    "source": "lse_vault",
                    "asof": candles[-1]["time"],
                    "candles": candles,
                }
        except (httpx.HTTPError, ValueError):
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
    for option in options:
        if not isinstance(option, dict):
            continue
        parsed = _parse_matrix_occ(str(option.get("option") or ""))
        if parsed is None:
            continue
        root, expiration, call_put, strike = parsed
        if expiration < today:
            continue
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
    return display_symbol, {
        "spot": round(spot, 2) if spot else None,
        "asof": data.get("last_trade_time") or today.isoformat(),
        "mult": 100,
        "opts": compact_options,
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
    async with httpx.AsyncClient(timeout=timeout) as client:
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


async def _fetch_matrix_symbol_catalog() -> list[dict[str, str]]:
    global _matrix_symbol_catalog_cache
    now = time.time()
    if (
        _matrix_symbol_catalog_cache is not None
        and now - _matrix_symbol_catalog_cache[0] < MATRIX_SYMBOL_CATALOG_CACHE_SECONDS
    ):
        return _matrix_symbol_catalog_cache[1]
    async with _matrix_symbol_catalog_lock:
        now = time.time()
        if (
            _matrix_symbol_catalog_cache is not None
            and now - _matrix_symbol_catalog_cache[0] < MATRIX_SYMBOL_CATALOG_CACHE_SECONDS
        ):
            return _matrix_symbol_catalog_cache[1]
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(
                MATRIX_CBOE_SYMBOL_DIRECTORY_URL,
                headers={"User-Agent": "Tripity Matrix symbol search (+https://trytripity.site)"},
            )
            response.raise_for_status()
        rows: list[dict[str, str]] = []
        for row in csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))):
            symbol = str(row.get(" Stock Symbol") or row.get("Stock Symbol") or "").upper().strip()
            name = str(row.get("Company Name") or "").strip()
            if not symbol or not name:
                continue
            try:
                symbol = _matrix_dynamic_symbol(symbol)
            except ValueError:
                continue
            rows.append({"symbol": symbol, "name": name})
        if not rows:
            raise ValueError("CBOE symbol directory returned no symbols")
        _matrix_symbol_catalog_cache = (time.time(), rows)
        return rows


async def _search_matrix_symbols(query: str, limit: int = 12) -> list[dict[str, str]]:
    text = query.strip()[:80]
    if not text:
        return []
    upper = text.upper()
    try:
        catalog = await _fetch_matrix_symbol_catalog()
    except (httpx.HTTPError, ValueError):
        catalog = []
    ranked: list[tuple[int, str, str, dict[str, str]]] = []
    for item in catalog:
        symbol = item["symbol"]
        name = item["name"]
        name_upper = name.upper()
        if symbol == upper:
            score = 0
        elif symbol.startswith(upper):
            score = 1
        elif upper in symbol:
            score = 2
        elif name_upper.startswith(upper):
            score = 3
        elif upper in name_upper:
            score = 4
        else:
            continue
        ranked.append((score, symbol, name_upper, item))
    ranked.sort(key=lambda item: (item[0], len(item[1]), item[1], item[2]))
    results = [item[3] for item in ranked[: max(1, min(limit, 20))]]
    if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,5}", upper) and not any(
        item["symbol"] == upper for item in results
    ):
        results.insert(0, {"symbol": upper, "name": "Load exact symbol"})
    return results[: max(1, min(limit, 20))]


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


def _matrix_lse_api_key() -> str:
    return (
        os.getenv("TRIPITY_MATRIX_LSE_API_KEY")
        or os.getenv("LSE_API_KEY")
        or ""
    ).strip()


def _matrix_lse_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "results"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _matrix_flow_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and abs(number) != float("inf"):
            return number
    return None


def _matrix_flow_epoch_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number < 10_000_000_000:
            number *= 1000
        return int(number)
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _matrix_flow_contract(row: dict[str, Any]) -> tuple[str, str] | None:
    ticker = str(row.get("ticker") or row.get("symbol") or "").removeprefix("O:")
    parsed = _parse_matrix_occ(ticker)
    if parsed is not None:
        _, expiration, call_put, _ = parsed
        return expiration.isoformat(), call_put
    expiration = row.get("expiration") or row.get("expiry") or row.get("expiration_date")
    contract_type = str(
        row.get("contract_type") or row.get("option_type") or row.get("right") or row.get("type") or ""
    ).lower()
    call_put = "C" if contract_type in {"c", "call"} else "P" if contract_type in {"p", "put"} else ""
    if not expiration or not call_put:
        return None
    return str(expiration)[:10], call_put


def _matrix_flow_direction(row: dict[str, Any], price: float | None, bid: float | None, ask: float | None) -> int:
    raw_side = str(
        row.get("trade_side_code") or row.get("trade_side") or row.get("side") or row.get("aggressor") or ""
    ).strip().upper().replace("-", "_").replace(" ", "_")
    if raw_side in {"A", "AA", "ASK", "BUY", "BOUGHT", "ABOVE_ASK", "AT_ASK"}:
        return 1
    if raw_side in {"B", "BB", "BID", "SELL", "SOLD", "BELOW_BID", "AT_BID"}:
        return -1
    if price is None or bid is None or ask is None or ask < bid:
        return 0
    midpoint = (bid + ask) / 2
    tolerance = max(0.0001, (ask - bid) * 0.01)
    if price > midpoint + tolerance:
        return 1
    if price < midpoint - tolerance:
        return -1
    return 0


def _matrix_lse_flow_payload(
    rows: list[dict[str, Any]],
    symbol: str,
    *,
    interval_seconds: int = 60,
    expirations: set[str] | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    buckets: dict[int, dict[str, Any]] = {}
    latest_asof = 0
    total_trades = classified_trades = 0
    total_premium = classified_premium = 0.0
    interval_ms = interval_seconds * 1000
    for row in rows:
        contract = _matrix_flow_contract(row)
        if contract is None:
            continue
        expiration, call_put = contract
        if expirations and expiration not in expirations:
            continue
        timestamp = _matrix_flow_epoch_ms(
            row.get("ts") or row.get("timestamp") or row.get("trade_time") or row.get("last_trade_at")
        )
        if timestamp is None:
            continue
        price = _matrix_flow_number(row, "price", "last_price", "option_price")
        size = _matrix_flow_number(row, "size", "volume", "contracts") or 0.0
        premium = _matrix_flow_number(row, "premium", "notional", "trade_premium")
        if premium is None and price is not None:
            premium = price * size * 100
        premium = max(0.0, premium or 0.0)
        if premium <= 0 and size <= 0:
            continue
        bid = _matrix_flow_number(row, "bid", "bid_price")
        ask = _matrix_flow_number(row, "ask", "ask_price")
        direction = _matrix_flow_direction(row, price, bid, ask)
        spot = _matrix_flow_number(row, "underlying_price", "stock_price", "spot")
        bucket_time = timestamp // interval_ms * interval_ms
        bucket = buckets.setdefault(bucket_time, {
            "time": bucket_time,
            "callPremium": 0.0, "putPremium": 0.0,
            "callVolume": 0.0, "putVolume": 0.0,
            "netCallPremium": 0.0, "netPutPremium": 0.0,
            "netCallVolume": 0.0, "netPutVolume": 0.0,
            "classifiedPremium": 0.0, "unknownPremium": 0.0,
            "midCallPremium": 0.0, "midPutPremium": 0.0,
            "spot": None, "trades": 0,
        })
        prefix = "call" if call_put == "C" else "put"
        bucket[f"{prefix}Premium"] += premium
        bucket[f"{prefix}Volume"] += size
        bucket["trades"] += 1
        if direction:
            bucket[f"net{prefix.title()}Premium"] += direction * premium
            bucket[f"net{prefix.title()}Volume"] += direction * size
            bucket["classifiedPremium"] += premium
            classified_trades += 1
            classified_premium += premium
        else:
            bucket["unknownPremium"] += premium
            bucket[f"mid{prefix.title()}Premium"] += premium
        if spot is not None and spot > 0:
            bucket["spot"] = spot
        total_trades += 1
        total_premium += premium
        latest_asof = max(latest_asof, timestamp)
    points = [buckets[key] for key in sorted(buckets)]
    last_spot = None
    for point in points:
        if point["spot"] is not None:
            last_spot = point["spot"]
        elif last_spot is not None:
            point["spot"] = last_spot
        for key, value in list(point.items()):
            if isinstance(value, float):
                point[key] = round(value, 4)
    coverage = classified_premium / total_premium if total_premium else 0.0
    return {
        "ok": True,
        "symbol": symbol,
        "source": "lse_options_flow",
        "asof": datetime.datetime.fromtimestamp(latest_asof / 1000, datetime.timezone.utc).isoformat() if latest_asof else None,
        "interval_seconds": interval_seconds,
        "points": points,
        "trades": total_trades,
        "classified_trades": classified_trades,
        "classification_coverage": round(coverage, 4),
        "premium": round(total_premium, 2),
        "partial": partial,
        "drift_method": "quote_rule_estimated" if classified_trades else "mid_market_unclassified",
    }


def _matrix_flow_db_path() -> Path:
    configured = os.getenv("TRIPITY_MATRIX_FLOW_DB", "").strip()
    if configured:
        return Path(configured)
    data_dir = Path("/data")
    return (data_dir if data_dir.exists() else Path.cwd()) / "matrix_flow.sqlite3"


def _matrix_retained_session_dates(reference: datetime.date | None = None) -> tuple[str, str, str]:
    session = reference or datetime.datetime.now(ZoneInfo("America/New_York")).date()
    while session.weekday() >= 5:
        session -= datetime.timedelta(days=1)
    dates: list[str] = []
    while len(dates) < 3:
        if session.weekday() < 5:
            dates.append(session.isoformat())
        session -= datetime.timedelta(days=1)
    return tuple(dates)  # today/latest session plus exactly two prior sessions


def _matrix_gex_history_sessions(reference: datetime.date | None = None) -> tuple[str, str, str]:
    """The three most recent COMPLETED trading sessions.

    On weekdays today's session is excluded (it may still be capturing);
    on weekends the most recent weekday already counts as completed.
    """
    now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    session = reference or now_et.date()
    # Skip the in-progress session only when today is itself a trading day.
    skip_current = session == now_et.date() and session.weekday() < 5
    while session.weekday() >= 5:
        session -= datetime.timedelta(days=1)
    if skip_current:
        session -= datetime.timedelta(days=1)
    dates: list[str] = []
    while len(dates) < 3:
        if session.weekday() < 5:
            dates.append(session.isoformat())
        session -= datetime.timedelta(days=1)
    return tuple(dates)


def _matrix_gex_retained_dates(reference: datetime.date | None = None) -> tuple[str, ...]:
    """Retention window for GEX snapshots: today plus the 3 completed sessions."""
    current = _matrix_session_date().isoformat() if reference is None else reference.isoformat()
    return (current, *_matrix_gex_history_sessions(reference))


def _matrix_flow_db_init() -> None:
    path = _matrix_flow_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=20) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS matrix_option_flow_minute (
                symbol TEXT NOT NULL,
                expiration TEXT NOT NULL,
                session_date TEXT NOT NULL,
                minute_ms INTEGER NOT NULL,
                call_premium REAL NOT NULL DEFAULT 0,
                put_premium REAL NOT NULL DEFAULT 0,
                call_volume REAL NOT NULL DEFAULT 0,
                put_volume REAL NOT NULL DEFAULT 0,
                net_call_premium REAL NOT NULL DEFAULT 0,
                net_put_premium REAL NOT NULL DEFAULT 0,
                net_call_volume REAL NOT NULL DEFAULT 0,
                net_put_volume REAL NOT NULL DEFAULT 0,
                classified_premium REAL NOT NULL DEFAULT 0,
                unknown_premium REAL NOT NULL DEFAULT 0,
                mid_call_premium REAL NOT NULL DEFAULT 0,
                mid_put_premium REAL NOT NULL DEFAULT 0,
                trades INTEGER NOT NULL DEFAULT 0,
                classified_trades INTEGER NOT NULL DEFAULT 0,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (symbol, expiration, minute_ms)
            )
        """)
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(matrix_option_flow_minute)")}
        for column in ("mid_call_premium", "mid_put_premium"):
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE matrix_option_flow_minute ADD COLUMN {column} REAL NOT NULL DEFAULT 0"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_matrix_flow_session ON matrix_option_flow_minute(symbol, session_date, minute_ms)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS matrix_gex_snapshot (
                symbol TEXT NOT NULL,
                session_date TEXT NOT NULL,
                minute_ms INTEGER NOT NULL,
                spot REAL NOT NULL,
                total_gex REAL NOT NULL DEFAULT 0,
                total_dex REAL NOT NULL DEFAULT 0,
                total_vex REAL NOT NULL DEFAULT 0,
                total_chex REAL NOT NULL DEFAULT 0,
                flip REAL,
                call_wall REAL,
                put_wall REAL,
                max_gamma_strike REAL,
                regime TEXT,
                atm_iv REAL,
                term_slope REAL,
                gex_scale REAL NOT NULL DEFAULT 0,
                vex_scale REAL NOT NULL DEFAULT 0,
                chex_scale REAL NOT NULL DEFAULT 0,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (symbol, minute_ms)
            )
        """)
        gex_columns = {row[1] for row in connection.execute("PRAGMA table_info(matrix_gex_snapshot)")}
        for column in ("total_vex", "total_chex", "gex_scale", "vex_scale", "chex_scale"):
            if column not in gex_columns:
                connection.execute(
                    f"ALTER TABLE matrix_gex_snapshot ADD COLUMN {column} REAL NOT NULL DEFAULT 0"
                )
        for column in ("atm_iv", "term_slope"):
            if column not in gex_columns:
                connection.execute(
                    f"ALTER TABLE matrix_gex_snapshot ADD COLUMN {column} REAL"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_matrix_gex_session ON matrix_gex_snapshot(symbol, session_date, minute_ms)"
        )
        # Regime journal: one pre-open call per (session_date, symbol), with
        # the engine's verdict frozen at save time and the realized outcome /
        # grades filled in after the close. Long-lived: NOT covered by the
        # retention deletes below.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS matrix_journal_entries (
                session_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                user_label TEXT NOT NULL,
                user_levels TEXT,
                user_notes TEXT,
                engine_label TEXT,
                engine_agreement INTEGER,
                engine_reasoning TEXT,
                call_wall REAL,
                put_wall REAL,
                outcome TEXT,
                grade INTEGER,
                engine_grade INTEGER,
                graded_ms INTEGER,
                PRIMARY KEY (session_date, symbol)
            )
        """)
        journal_columns = {row[1] for row in connection.execute("PRAGMA table_info(matrix_journal_entries)")}
        for column in ("engine_reasoning", "user_levels", "user_notes"):
            if column not in journal_columns:
                connection.execute(
                    f"ALTER TABLE matrix_journal_entries ADD COLUMN {column} TEXT"
                )
        for column in ("engine_agreement", "grade", "engine_grade", "graded_ms", "created_ms"):
            if column not in journal_columns:
                connection.execute(
                    f"ALTER TABLE matrix_journal_entries ADD COLUMN {column} INTEGER"
                )
        for column in ("call_wall", "put_wall"):
            if column not in journal_columns:
                connection.execute(
                    f"ALTER TABLE matrix_journal_entries ADD COLUMN {column} REAL"
                )
        retained = _matrix_retained_session_dates()
        connection.execute(
            "DELETE FROM matrix_option_flow_minute WHERE session_date NOT IN (?, ?, ?)",
            retained,
        )
        gex_retained = _matrix_gex_retained_dates()
        connection.execute(
            f"DELETE FROM matrix_gex_snapshot WHERE session_date NOT IN ({','.join('?' for _ in gex_retained)})",
            gex_retained,
        )
        connection.commit()


def _matrix_flow_db_flush(rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    _matrix_flow_db_init()
    with sqlite3.connect(_matrix_flow_db_path(), timeout=20) as connection:
        connection.executemany("""
            INSERT INTO matrix_option_flow_minute (
                symbol, expiration, session_date, minute_ms,
                call_premium, put_premium, call_volume, put_volume,
                net_call_premium, net_put_premium, net_call_volume, net_put_volume,
                classified_premium, unknown_premium, mid_call_premium, mid_put_premium,
                trades, classified_trades, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, expiration, minute_ms) DO UPDATE SET
                call_premium=call_premium+excluded.call_premium,
                put_premium=put_premium+excluded.put_premium,
                call_volume=call_volume+excluded.call_volume,
                put_volume=put_volume+excluded.put_volume,
                net_call_premium=net_call_premium+excluded.net_call_premium,
                net_put_premium=net_put_premium+excluded.net_put_premium,
                net_call_volume=net_call_volume+excluded.net_call_volume,
                net_put_volume=net_put_volume+excluded.net_put_volume,
                classified_premium=classified_premium+excluded.classified_premium,
                unknown_premium=unknown_premium+excluded.unknown_premium,
                mid_call_premium=mid_call_premium+excluded.mid_call_premium,
                mid_put_premium=mid_put_premium+excluded.mid_put_premium,
                trades=trades+excluded.trades,
                classified_trades=classified_trades+excluded.classified_trades,
                updated_at_ms=MAX(updated_at_ms, excluded.updated_at_ms)
        """, rows)
        connection.commit()


def _matrix_flow_db_payload(
    symbol: str,
    interval_seconds: int,
    session_date: str,
    expirations: tuple[str, ...],
) -> dict[str, Any] | None:
    path = _matrix_flow_db_path()
    if not path.exists():
        return None
    interval_ms = interval_seconds * 1000
    filters = "symbol=? AND session_date=?"
    params: list[Any] = [interval_ms, interval_ms, symbol, session_date]
    if expirations:
        filters += f" AND expiration IN ({','.join('?' for _ in expirations)})"
        params.extend(expirations)
    query = f"""
        SELECT (minute_ms / ?) * ? AS bucket_ms,
               SUM(call_premium), SUM(put_premium), SUM(call_volume), SUM(put_volume),
               SUM(net_call_premium), SUM(net_put_premium), SUM(net_call_volume), SUM(net_put_volume),
               SUM(classified_premium), SUM(unknown_premium),
               SUM(mid_call_premium), SUM(mid_put_premium), SUM(trades), SUM(classified_trades),
               MAX(updated_at_ms)
        FROM matrix_option_flow_minute WHERE {filters}
        GROUP BY bucket_ms ORDER BY bucket_ms
    """
    with sqlite3.connect(path, timeout=20) as connection:
        rows = connection.execute(query, params).fetchall()
    if not rows:
        return None
    points = []
    for row in rows:
        points.append({
            "time": row[0], "callPremium": row[1], "putPremium": row[2],
            "callVolume": row[3], "putVolume": row[4],
            "netCallPremium": row[5], "netPutPremium": row[6],
            "netCallVolume": row[7], "netPutVolume": row[8],
            "classifiedPremium": row[9], "unknownPremium": row[10],
            "midCallPremium": row[11], "midPutPremium": row[12],
            "trades": row[13], "spot": None,
        })
    total_premium = sum(float(point["callPremium"] or 0) + float(point["putPremium"] or 0) for point in points)
    classified_premium = sum(float(point["classifiedPremium"] or 0) for point in points)
    session_start = datetime.datetime.fromisoformat(f"{session_date}T09:30:00").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    from_open = int(rows[0][0]) <= int(session_start.timestamp() * 1000) + 60_000
    return {
        "ok": True, "symbol": symbol, "source": "lse_websocket_collector",
        "asof": datetime.datetime.fromtimestamp(rows[-1][15] / 1000, datetime.timezone.utc).isoformat(),
        "interval_seconds": interval_seconds, "points": points,
        "trades": sum(int(point["trades"] or 0) for point in points),
        "classified_trades": sum(int(row[14] or 0) for row in rows),
        "classification_coverage": round(classified_premium / total_premium, 4) if total_premium else 0,
        "premium": round(total_premium, 2), "partial": False,
        "from_open": from_open,
        "drift_method": "lee_ready_quote_tick_estimated" if classified_premium else "mid_market_unclassified",
    }


def _merge_matrix_flow_payloads(rest: dict[str, Any], collected: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(rest)
    collector = _matrix_flow_collector
    merged["collector"] = collector.status() if collector is not None else {"state": "disabled"}
    if collected is None:
        return merged
    by_time = {int(point["time"]): point for point in merged.get("points") or []}
    # Collector rows own minutes after the server started. This avoids double
    # counting the same prints from REST bootstrap and the live stream.
    by_time.update({int(point["time"]): point for point in collected.get("points") or []})
    points = [by_time[key] for key in sorted(by_time)]
    total_premium = sum(float(point.get("callPremium") or 0) + float(point.get("putPremium") or 0) for point in points)
    classified_premium = sum(float(point.get("classifiedPremium") or 0) for point in points)
    merged.update({
        "source": "lse_rest+websocket_collector", "points": points,
        "trades": sum(int(point.get("trades") or 0) for point in points),
        "premium": round(total_premium, 2),
        "partial": bool(rest.get("partial")) and not bool(collected.get("from_open")),
        "classification_coverage": round(classified_premium / total_premium, 4) if total_premium else 0,
        "drift_method": "lee_ready_quote_tick_estimated" if classified_premium else "mid_market_unclassified",
        "collector_session": collected.get("asof"),
        "collector_from_open": bool(collected.get("from_open")),
    })
    return merged


def _matrix_session_date(offset: int = 0) -> datetime.date:
    now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    session = now_et.date()
    while session.weekday() >= 5:
        session -= datetime.timedelta(days=1)
    for _ in range(max(0, offset)):
        session -= datetime.timedelta(days=1)
        while session.weekday() >= 5:
            session -= datetime.timedelta(days=1)
    return session


async def _fetch_matrix_flow_history(
    symbol: str,
    interval: str,
    expirations: tuple[str, ...],
    session: int | str,
) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    if symbol not in MATRIX_LSE_SYMBOLS:
        raise ValueError("Net Drift & Flow is available for SPY and QQQ")
    if interval not in MATRIX_FLOW_INTERVALS:
        raise ValueError("Flow interval must be 1m, 5m, or 15m")
    if isinstance(session, int):
        if session not in {1, 2}:
            raise ValueError("Historical flow supports one or two sessions back")
        session_date = _matrix_session_date(session).isoformat()
    else:
        try:
            session_date = datetime.date.fromisoformat(session).isoformat()
        except MatrixDynamicRateLimitError as exc:
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=429,
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"retry-after", b"60"),
                ],
            )
        except ValueError as exc:
            raise ValueError("Flow session_date must use YYYY-MM-DD") from exc
        if session_date not in _matrix_retained_session_dates():
            raise ValueError("Flow session_date is outside the three retained trading sessions")
    collected = await asyncio.to_thread(
        _matrix_flow_db_payload,
        symbol,
        MATRIX_FLOW_INTERVALS[interval],
        session_date,
        tuple(sorted(expirations)),
    )
    if collected is not None:
        collected["collector"] = _matrix_flow_collector.status() if _matrix_flow_collector else {"state": "disabled"}
        collected["session_date"] = session_date
        collected["history_available"] = True
        return collected
    session_close = datetime.datetime.fromisoformat(f"{session_date}T16:00:00").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    return {
        "ok": True, "symbol": symbol, "source": "lse_websocket_collector_history",
        "asof": session_close.astimezone(datetime.timezone.utc).isoformat(),
        "session_date": session_date, "interval_seconds": MATRIX_FLOW_INTERVALS[interval],
        "points": [], "trades": 0, "classified_trades": 0,
        "classification_coverage": 0, "premium": 0, "partial": False,
        "history_available": False, "drift_method": "lee_ready_quote_tick_estimated",
        "collector": _matrix_flow_collector.status() if _matrix_flow_collector else {"state": "disabled"},
    }


class MatrixFlowCollector:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.pending: dict[tuple[str, str, str, int], dict[str, float]] = {}
        self.state = "starting"
        self.detail = ""
        self.connected_at: str | None = None
        self.last_tick: str | None = None
        self.ticks = 0
        self.underlying_ticks = 0
        self.duplicates = 0
        self.reconnects = 0
        self.connection_gaps: list[dict[str, Any]] = []
        self.latest_spot: dict[str, float] = {}
        self.latest_spot_asof: dict[str, str] = {}
        self.client: Any | None = None
        self.dynamic_symbol: str | None = None
        self._recent_signatures: dict[tuple[Any, ...], float] = {}
        self._last_contract_volume: dict[str, float] = {}
        self._volume_comparisons = 0
        self._volume_nondecreasing = 0
        self._ever_authenticated = False
        self._disconnected_at: datetime.datetime | None = None
        self._last_flush = time.monotonic()
        self._last_contract_price: dict[str, float] = {}
        self._last_contract_direction: dict[str, int] = {}

    def status(self) -> dict[str, Any]:
        now = datetime.datetime.now(datetime.timezone.utc)
        now_et = now.astimezone(ZoneInfo("America/New_York"))
        market_minute = now_et.hour * 60 + now_et.minute
        market_open = now_et.weekday() < 5 and 570 <= market_minute <= 960
        last_tick_ms = _matrix_flow_epoch_ms(self.last_tick)
        last_tick_age = max(0, (now.timestamp() * 1000 - last_tick_ms) / 1000) if last_tick_ms else None
        volume_ratio = self._volume_nondecreasing / self._volume_comparisons if self._volume_comparisons else None
        volume_semantics = "insufficient_samples"
        if self._volume_comparisons >= 100:
            volume_semantics = "suspected_cumulative" if (volume_ratio or 0) >= 0.97 else "trade_size_likely"
        if not market_open:
            quality = "market_closed"
        elif self.state != "connected":
            quality = "disconnected"
        elif market_minute >= 572 and self.ticks == 0:
            quality = "no_option_ticks"
        elif last_tick_age is not None and last_tick_age > 120:
            quality = "stale"
        elif volume_semantics == "suspected_cumulative":
            quality = "volume_warning"
        else:
            quality = "healthy"
        return {
            "state": self.state, "detail": self.detail, "connected_at": self.connected_at,
            "last_tick": self.last_tick, "last_tick_age_seconds": round(last_tick_age, 1) if last_tick_age is not None else None,
            "ticks": self.ticks, "underlying_ticks": self.underlying_ticks,
            "duplicates": self.duplicates, "reconnects": self.reconnects,
            "gaps": self.connection_gaps[-8:], "latest_spot": self.latest_spot,
            "quality": quality, "market_open": market_open,
            "volume_semantics": volume_semantics,
            "volume_nondecreasing_ratio": round(volume_ratio, 4) if volume_ratio is not None else None,
            "volume_samples": self._volume_comparisons,
            "dynamic_symbol": self.dynamic_symbol,
        }

    def set_active_symbol(self, symbol: str) -> None:
        symbol = _matrix_dynamic_symbol(symbol)
        previous = self.dynamic_symbol
        if previous == symbol:
            return
        self.dynamic_symbol = symbol
        if previous:
            self.latest_spot.pop(previous, None)
            self.latest_spot_asof.pop(previous, None)
        client = self.client
        if client is not None:
            if previous:
                client.unsubscribe([previous])
            client.subscribe([symbol])

    def clear_active_symbol(self) -> None:
        previous = self.dynamic_symbol
        if not previous:
            return
        self.dynamic_symbol = None
        self.latest_spot.pop(previous, None)
        self.latest_spot_asof.pop(previous, None)
        if self.client is not None:
            self.client.unsubscribe([previous])

    def _on_authenticated(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        if self._ever_authenticated:
            self.reconnects += 1
        if self._disconnected_at is not None:
            self.connection_gaps.append({
                "from": self._disconnected_at.isoformat(),
                "to": now.isoformat(),
                "seconds": round((now - self._disconnected_at).total_seconds(), 2),
            })
            self._disconnected_at = None
        self._ever_authenticated = True
        self.state = "connected"
        self.detail = "SPY and QQQ subscribed · quote + tick classification active"
        self.connected_at = now.isoformat()

    def _on_error(self, message: Any) -> None:
        self.state = "error"
        self.detail = str(message)[:240]
        self._disconnected_at = self._disconnected_at or datetime.datetime.now(datetime.timezone.utc)

    def observe(self, tick: Any) -> None:
        symbol = str(getattr(tick, "underlying", "") or "").upper()
        right = str(getattr(tick, "right", "") or "").lower()
        tick_symbol = str(getattr(tick, "symbol", "") or "").upper()
        # Underlying price tick: map streamed symbol (SPY, QQQ, or an index
        # catalog name like SPX500/USD) to the dashboard symbol.
        is_dynamic = self.dynamic_symbol is not None and tick_symbol == self.dynamic_symbol
        display_symbol = tick_symbol if tick_symbol in MATRIX_LSE_SYMBOLS or is_dynamic else MATRIX_LSE_INDEX_STREAM_SYMBOLS.get(tick_symbol)
        if not symbol and display_symbol:
            try:
                spot = float(getattr(tick, "price", 0) or 0)
            except (TypeError, ValueError):
                spot = 0
            if spot > 0:
                self.latest_spot[display_symbol] = spot
                timestamp = _matrix_flow_epoch_ms(getattr(tick, "timestamp", None))
                self.latest_spot_asof[display_symbol] = (
                    datetime.datetime.fromtimestamp(timestamp / 1000, datetime.timezone.utc).isoformat()
                    if timestamp is not None
                    else datetime.datetime.now(datetime.timezone.utc).isoformat()
                )
                self.underlying_ticks += 1
            return
        if symbol not in MATRIX_LSE_SYMBOLS or right not in {"call", "put"}:
            return
        timestamp = _matrix_flow_epoch_ms(getattr(tick, "timestamp", None))
        expiry = getattr(tick, "expiry", None)
        if timestamp is None or expiry is None:
            return
        instant = datetime.datetime.fromtimestamp(timestamp / 1000, ZoneInfo("America/New_York"))
        market_minute = instant.hour * 60 + instant.minute
        if instant.weekday() >= 5 or market_minute < 570 or market_minute > 960:
            return
        try:
            price = float(getattr(tick, "price", 0) or 0)
            size = max(0.0, float(getattr(tick, "volume", 0) or 0))
        except (TypeError, ValueError):
            return
        if price <= 0 or size <= 0:
            return
        signature = (
            tick_symbol, timestamp, round(price, 8), round(size, 4),
            getattr(tick, "bid", None), getattr(tick, "ask", None),
        )
        if signature in self._recent_signatures:
            self.duplicates += 1
            return
        self._recent_signatures[signature] = time.monotonic()
        if len(self._recent_signatures) > 20_000:
            cutoff = time.monotonic() - 900
            self._recent_signatures = {key: seen for key, seen in self._recent_signatures.items() if seen >= cutoff}
        premium = price * size * 100
        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)
        direction = _matrix_flow_direction(
            {}, price,
            float(bid) if bid is not None else None,
            float(ask) if ask is not None else None,
        )
        contract_key = str(getattr(tick, "symbol", "") or "") or (
            f"{symbol}|{expiry.isoformat()}|{right}|{getattr(tick, 'strike', '')}"
        )
        previous_volume = self._last_contract_volume.get(contract_key)
        if previous_volume is not None:
            self._volume_comparisons += 1
            if size >= previous_volume:
                self._volume_nondecreasing += 1
        self._last_contract_volume[contract_key] = size
        if not direction:
            previous_price = self._last_contract_price.get(contract_key)
            tolerance = max(0.0001, price * 0.00001)
            if previous_price is not None and price > previous_price + tolerance:
                direction = 1
            elif previous_price is not None and price < previous_price - tolerance:
                direction = -1
            elif previous_price is not None:
                direction = self._last_contract_direction.get(contract_key, 0)
        self._last_contract_price[contract_key] = price
        if direction:
            self._last_contract_direction[contract_key] = direction
        call_put = "C" if right == "call" else "P"
        key = (symbol, expiry.isoformat(), instant.date().isoformat(), timestamp // 60000 * 60000)
        row = self.pending.setdefault(key, {
            "call_premium": 0, "put_premium": 0, "call_volume": 0, "put_volume": 0,
            "net_call_premium": 0, "net_put_premium": 0,
            "net_call_volume": 0, "net_put_volume": 0,
            "classified_premium": 0, "unknown_premium": 0,
            "mid_call_premium": 0, "mid_put_premium": 0,
            "trades": 0, "classified_trades": 0, "updated_at_ms": 0,
        })
        prefix = "call" if call_put == "C" else "put"
        row[f"{prefix}_premium"] += premium
        row[f"{prefix}_volume"] += size
        row["trades"] += 1
        if direction:
            row[f"net_{prefix}_premium"] += direction * premium
            row[f"net_{prefix}_volume"] += direction * size
            row["classified_premium"] += premium
            row["classified_trades"] += 1
        else:
            row["unknown_premium"] += premium
            row[f"mid_{prefix}_premium"] += premium
        row["updated_at_ms"] = max(row["updated_at_ms"], timestamp)
        self.last_tick = datetime.datetime.fromtimestamp(timestamp / 1000, datetime.timezone.utc).isoformat()
        self.ticks += 1

    async def flush(self) -> None:
        if not self.pending:
            return
        pending, self.pending = self.pending, {}
        fields = (
            "call_premium", "put_premium", "call_volume", "put_volume",
            "net_call_premium", "net_put_premium", "net_call_volume", "net_put_volume",
            "classified_premium", "unknown_premium", "mid_call_premium", "mid_put_premium",
            "trades", "classified_trades", "updated_at_ms",
        )
        rows = [(*key, *(values[field] for field in fields)) for key, values in pending.items()]
        await asyncio.to_thread(_matrix_flow_db_flush, rows)
        self._last_flush = time.monotonic()

    async def run(self) -> None:
        from lse import LSE
        await asyncio.to_thread(_matrix_flow_db_init)
        base_stream_symbols = list(MATRIX_LSE_SYMBOLS)
        if os.getenv("TRIPITY_MATRIX_STREAM_INDICES", "1").strip().lower() not in {"0", "false", "off", "no"}:
            base_stream_symbols += list(MATRIX_LSE_INDEX_STREAM_SYMBOLS)
        while True:
            stream_symbols = [*base_stream_symbols]
            if self.dynamic_symbol and self.dynamic_symbol not in stream_symbols:
                stream_symbols.append(self.dynamic_symbol)
            client = LSE(api_key=self.api_key)
            self.client = client
            client.subscribe_options(list(MATRIX_LSE_SYMBOLS))
            client.on("authenticated", self._on_authenticated)
            client.on("error", self._on_error)
            self.state = "connecting"
            try:
                async for tick in client.stream_async(stream_symbols, reconnect=False):
                    self.observe(tick)
                    if time.monotonic() - self._last_flush >= 2:
                        await self.flush()
            except asyncio.CancelledError:
                await client.disconnect_async()
                await self.flush()
                raise
            except Exception as exc:  # noqa: BLE001
                self.state = "error"
                self.detail = str(exc)[:240]
                self._disconnected_at = self._disconnected_at or datetime.datetime.now(datetime.timezone.utc)
            finally:
                if self.client is client:
                    self.client = None
            await self.flush()
            if self.state == "connected":
                self.state = "reconnecting"
                self.detail = "WebSocket ended; reconnect scheduled"
                self._disconnected_at = self._disconnected_at or datetime.datetime.now(datetime.timezone.utc)
            await asyncio.sleep(3)


async def _fetch_matrix_flow(
    symbol: str,
    interval: str,
    expirations: tuple[str, ...] = (),
) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    if symbol not in MATRIX_LSE_SYMBOLS:
        raise ValueError("Net Drift & Flow is available for SPY and QQQ")
    if interval not in MATRIX_FLOW_INTERVALS:
        raise ValueError("Flow interval must be 1m, 5m, or 15m")
    api_key = _matrix_lse_api_key()
    if not api_key:
        raise ValueError("LSE API key is not configured")
    cache_key = (symbol, interval, tuple(sorted(expirations)))
    sorted_expirations = tuple(sorted(expirations))

    # Once the persistent collector owns the session from the opening minute,
    # SQLite is the complete live source. This keeps browser refreshes entirely
    # on Tripity and reserves LSE REST for bootstrap/backfill only.
    current_session = _matrix_session_date().isoformat()
    collected_now = await asyncio.to_thread(
        _matrix_flow_db_payload,
        symbol,
        MATRIX_FLOW_INTERVALS[interval],
        current_session,
        sorted_expirations,
    )
    collector_status = _matrix_flow_collector.status() if _matrix_flow_collector else {"state": "disabled"}
    if collected_now is not None and collected_now.get("from_open") and collector_status.get("state") == "connected":
        collected_now["collector"] = collector_status
        collected_now["upstream_mode"] = "websocket_db"
        collected_now["rest_refresh_seconds"] = MATRIX_FLOW_CACHE_SECONDS
        return collected_now

    async def include_collector(payload: dict[str, Any]) -> dict[str, Any]:
        asof_ms = _matrix_flow_epoch_ms(payload.get("asof"))
        anchor = datetime.datetime.fromtimestamp(
            (asof_ms or int(time.time() * 1000)) / 1000,
            ZoneInfo("America/New_York"),
        )
        collected = await asyncio.to_thread(
            _matrix_flow_db_payload,
            symbol,
            MATRIX_FLOW_INTERVALS[interval],
            anchor.date().isoformat(),
            sorted_expirations,
        )
        merged = _merge_matrix_flow_payloads(payload, collected)
        merged["upstream_mode"] = "rest_bootstrap+websocket_db"
        merged["rest_refresh_seconds"] = MATRIX_FLOW_CACHE_SECONDS
        return merged

    now = time.time()
    cached = _matrix_flow_cache.get(cache_key)
    if cached is not None and now - cached[0] < MATRIX_FLOW_CACHE_SECONDS:
        return await include_collector(cached[1])
    async with _matrix_flow_lock:
        now = time.time()
        cached = _matrix_flow_cache.get(cache_key)
        if cached is not None and now - cached[0] < MATRIX_FLOW_CACHE_SECONDS:
            return await include_collector(cached[1])
        et_now = datetime.datetime.now(ZoneInfo("America/New_York"))
        session_start = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        if et_now.weekday() >= 5:
            session_start -= datetime.timedelta(days=1)
        while session_start.weekday() >= 5:
            session_start -= datetime.timedelta(days=1)
        params = {
            "underlying": symbol,
            # The vault accepts UTC ISO timestamps without a suffix here.
            "start": session_start.astimezone(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
            "order": "desc",
            "limit": "5000",
        }
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                MATRIX_LSE_FLOW_URL,
                params=params,
                headers={"x-api-key": api_key, "User-Agent": "Tripity Matrix options flow (+https://trytripity.site)"},
            )
            response.raise_for_status()
            rows = _matrix_lse_rows(response.json())
        payload = _matrix_lse_flow_payload(
            rows,
            symbol,
            interval_seconds=MATRIX_FLOW_INTERVALS[interval],
            expirations=set(expirations) or None,
            partial=len(rows) >= 5000,
        )
        _matrix_flow_cache[cache_key] = (time.time(), payload)
        return await include_collector(payload)


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
        for source, target, digits in (
            ("iv", "iv", 6),
            ("gamma", "g", 8),
            ("delta", "d", 6),
        ):
            value = live.get(source)
            if value is not None:
                option[target] = round(float(value), digits)
        volume = live.get("volume")
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
                headers={"x-api-key": api_key, "User-Agent": "Tripity Matrix market data (+https://trytripity.site)"},
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

        async def fetch_one(client: httpx.AsyncClient, display_symbol: str, lse_symbol: str):
            async with semaphore:
                response = await client.get(
                    MATRIX_LSE_CANDLES_URL,
                    params={"symbol": lse_symbol, "timeframe": "1m", "order": "desc", "limit": "1"},
                    headers={
                        "x-api-key": api_key,
                        "User-Agent": "Tripity Matrix market data (+https://trytripity.site)",
                    },
                )
                response.raise_for_status()
            rows = _matrix_lse_rows(response.json())
            if not rows:
                raise ValueError(f"LSE returned no price for {display_symbol}")
            row = rows[0]
            asof = str(row.get("ts") or row.get("timestamp") or "")
            if asof and not re.search(r"Z$|[+-]\d{2}:?\d{2}$", asof):
                asof = asof.replace(" ", "T") + "Z"
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
    data = deepcopy(await _fetch_matrix_market_data_cached())
    for symbol, live in (await _fetch_matrix_lse_spots()).items():
        record = data.get(symbol)
        if not isinstance(record, dict):
            continue
        record["oi_asof"] = record.get("oi_asof") or record.get("asof")
        record["oi_source"] = "cboe_daily"
        record["spot"] = live["spot"]
        record["asof"] = live["asof"]
        record["price_asof"] = live["asof"]
        record["price_source"] = MATRIX_LSE_PRICE_SYMBOLS[symbol]
        if symbol in {"SPX", "NDX"}:
            record["source"] = "lse_live_index+cboe_oi"
        elif record.get("source") == "tripity_cboe":
            record["source"] = "lse_live+cboe_oi"
    return data


# -----------------------------------------------------------------------------
# Matrix GEX snapshots (live levels + per-minute history)
# Greeks/exposure math lives in the canonical engine (matrix_gex.py); this
# module only shapes snapshots for storage. Keep payload fields stable.
# -----------------------------------------------------------------------------
def _compute_matrix_gex_snapshot(
    symbol: str,
    record: dict[str, Any],
    spot: float,
    valuation_ms: int,
) -> dict[str, Any]:
    """Aggregate per-strike GEX/DEX/VEX/CHEX and derive flip/walls/regime.

    Mirrors the "full" mode of the analytics engine in matrix.js:
    GEX = gamma * OI * mult * spot^2 * 0.01 (calls +, puts -). VEX/CHEX
    totals follow the engine conventions (calls +, puts -; CHEX per
    trading day with the put-side single negation).
    """
    mult = float(record.get("mult") or 100)
    options = record.get("opts") or []
    strikes = matrix_gex.aggregate_strikes(options, spot, mult, valuation_ms, include_vc=True)

    total_call_gex = sum(row["call_gex"] for row in strikes)
    total_put_gex = sum(row["put_gex"] for row in strikes)
    total_gex = total_call_gex + total_put_gex
    total_dex = sum(row["net_dex"] for row in strikes)
    total_vex = sum(row["net_vex"] for row in strikes)
    total_chex = sum(row["net_chex"] for row in strikes)

    lo, hi = spot * 0.8, spot * 1.2
    levels = [lo + (hi - lo) * i / 59 for i in range(60)]
    flip = matrix_gex.zero_gamma_flip(
        levels,
        matrix_gex.gamma_profile(options, levels, mult, valuation_ms, min_oi=10),
    )

    call_wall = put_wall = max_gamma_strike = None
    cw_g, pw_g, max_abs = float("-inf"), float("inf"), 0.0
    for row in strikes:
        K, net = row["strike"], row["net_gex"]
        if net > cw_g:
            cw_g, call_wall = net, K
        if net < pw_g:
            pw_g, put_wall = net, K
        if abs(net) > max_abs:
            max_abs, max_gamma_strike = abs(net), K

    strong_mag = abs(total_call_gex) + abs(total_put_gex)
    if total_gex > strong_mag * 0.04:
        regime = "positive_gamma"
    elif total_gex < -strong_mag * 0.04:
        regime = "negative_gamma"
    else:
        regime = "neutral"

    # ATM IV term structure (front/back + slope) — persisted with each
    # snapshot so history replay can reactivate the engine's VEX leg.
    term = matrix_regime.atm_iv_term_structure(options, spot, valuation_ms)

    return {
        "spot": spot,
        "total_gex": total_gex,
        "total_dex": total_dex,
        "total_vex": total_vex,
        "total_chex": total_chex,
        # Additive: per-side absolute sums, used by the regime engine as each
        # force's noise deadzone; persisted to the snapshot table so replay
        # applies the same deadzones as the live engine.
        "gex_scale": strong_mag,
        "vex_scale": sum(abs(row["call_vex"]) + abs(row["put_vex"]) for row in strikes),
        "chex_scale": sum(abs(row["call_chex"]) + abs(row["put_chex"]) for row in strikes),
        # Additive: ATM vol context (None when the chain has no usable IV).
        "atm_iv": term["front_iv"],
        "term_slope": term["slope"],
        "flip": flip,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "max_gamma_strike": max_gamma_strike,
        "regime": regime,
    }


def _matrix_gex_db_insert(rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    _matrix_flow_db_init()
    with sqlite3.connect(_matrix_flow_db_path(), timeout=20) as connection:
        connection.executemany("""
            INSERT INTO matrix_gex_snapshot (
                symbol, session_date, minute_ms, spot, total_gex, total_dex,
                total_vex, total_chex,
                flip, call_wall, put_wall, max_gamma_strike, regime,
                atm_iv, term_slope, gex_scale, vex_scale, chex_scale, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, minute_ms) DO UPDATE SET
                spot=excluded.spot,
                total_gex=excluded.total_gex,
                total_dex=excluded.total_dex,
                total_vex=excluded.total_vex,
                total_chex=excluded.total_chex,
                flip=excluded.flip,
                call_wall=excluded.call_wall,
                put_wall=excluded.put_wall,
                max_gamma_strike=excluded.max_gamma_strike,
                regime=excluded.regime,
                atm_iv=excluded.atm_iv,
                term_slope=excluded.term_slope,
                gex_scale=excluded.gex_scale,
                vex_scale=excluded.vex_scale,
                chex_scale=excluded.chex_scale,
                updated_at_ms=excluded.updated_at_ms
        """, rows)
        connection.commit()


def _matrix_gex_db_payload(symbol: str, session_date: str) -> dict[str, Any] | None:
    path = _matrix_flow_db_path()
    if not path.exists():
        return None
    # Tolerate legacy DBs missing the newer columns: select what exists and
    # fill the rest as None (the neutral replay fallback).
    all_columns = (
        "minute_ms", "spot", "total_gex", "total_dex", "total_vex", "total_chex",
        "flip", "call_wall", "put_wall", "max_gamma_strike", "regime",
        "atm_iv", "term_slope", "gex_scale", "vex_scale", "chex_scale",
        "updated_at_ms",
    )
    with sqlite3.connect(path, timeout=20) as connection:
        available = {row[1] for row in connection.execute("PRAGMA table_info(matrix_gex_snapshot)")}
        columns = [c for c in all_columns if c in available]
        rows = connection.execute(
            f"""
            SELECT {', '.join(columns)}
            FROM matrix_gex_snapshot
            WHERE symbol = ? AND session_date = ?
            ORDER BY minute_ms
            """,
            (symbol, session_date),
        ).fetchall()
    if not rows:
        return None
    points = []
    for row in rows:
        record = {name: row[i] for i, name in enumerate(columns)}
        points.append({
            "time": record.get("minute_ms"),
            "spot": record.get("spot"),
            "totalGex": record.get("total_gex"),
            "totalDex": record.get("total_dex"),
            "totalVex": record.get("total_vex"),
            "totalChex": record.get("total_chex"),
            "flip": record.get("flip"),
            "callWall": record.get("call_wall"),
            "putWall": record.get("put_wall"),
            "maxGammaStrike": record.get("max_gamma_strike"),
            "regime": record.get("regime"),
            # Phase-5 additions (additive; None on legacy rows).
            "atmIv": record.get("atm_iv"),
            "termSlope": record.get("term_slope"),
            "gexScale": record.get("gex_scale"),
            "vexScale": record.get("vex_scale"),
            "chexScale": record.get("chex_scale"),
        })
    return {
        "ok": True,
        "symbol": symbol,
        "source": "matrix_gex_snapshot_store",
        "asof": datetime.datetime.fromtimestamp(rows[-1][columns.index("updated_at_ms")] / 1000, datetime.timezone.utc).isoformat(),
        "interval_seconds": MATRIX_GEX_SNAPSHOT_INTERVAL_SECONDS,
        "session_date": session_date,
        "points": points,
        "history_available": True,
    }


async def _fetch_matrix_gex_history(
    symbol: str,
    session: int | str,
) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    if symbol not in MATRIX_LSE_PRICE_SYMBOLS:
        raise ValueError("GEX history is available for NDX, SPX, SPY, and QQQ")
    sessions = _matrix_gex_history_sessions()
    if isinstance(session, int):
        if session not in {1, 2, 3}:
            raise ValueError("GEX history supports one to three sessions back")
        session_date = sessions[session - 1]
    else:
        try:
            session_date = datetime.date.fromisoformat(session).isoformat()
        except ValueError as exc:
            raise ValueError("GEX session_date must use YYYY-MM-DD") from exc
        if session_date not in sessions:
            raise ValueError("GEX session_date is outside the three retained prior sessions")
    collected = await asyncio.to_thread(_matrix_gex_db_payload, symbol, session_date)
    if collected is not None:
        collected["available_sessions"] = list(sessions)
        return collected
    session_close = datetime.datetime.fromisoformat(f"{session_date}T16:00:00").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    return {
        "ok": True,
        "symbol": symbol,
        "source": "matrix_gex_snapshot_store",
        "asof": session_close.astimezone(datetime.timezone.utc).isoformat(),
        "interval_seconds": MATRIX_GEX_SNAPSHOT_INTERVAL_SECONDS,
        "session_date": session_date,
        "points": [],
        "history_available": False,
        "available_sessions": list(sessions),
    }


def _matrix_market_open_now() -> bool:
    now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    minute = now_et.hour * 60 + now_et.minute
    return 570 <= minute <= 960


async def _matrix_gex_snapshot_loop() -> None:
    """Capture per-minute GEX snapshots during ET market hours.

    Uses the already-cached market data (zero extra upstream API calls); spot
    is overridden with the WebSocket collector's latest tick when available.
    """
    await asyncio.to_thread(_matrix_flow_db_init)
    while True:
        now = time.time()
        await asyncio.sleep(MATRIX_GEX_SNAPSHOT_INTERVAL_SECONDS - (now % MATRIX_GEX_SNAPSHOT_INTERVAL_SECONDS))
        if not _matrix_market_open_now():
            continue
        try:
            data = await _fetch_matrix_market_data()
        except Exception as exc:  # noqa: BLE001
            print(f"matrix gex snapshot: market data unavailable: {exc}")
            continue
        collector = _matrix_flow_collector
        live_spots = dict(collector.latest_spot) if collector is not None else {}
        updated_ms = int(time.time() * 1000)
        minute_ms = updated_ms // 60000 * 60000
        session_date = _matrix_session_date().isoformat()
        rows: list[tuple[Any, ...]] = []
        for symbol, record in data.items():
            if not isinstance(record, dict):
                continue
            spot = live_spots.get(symbol) or record.get("spot")
            if not spot:
                continue
            try:
                snap = _compute_matrix_gex_snapshot(symbol, record, float(spot), updated_ms)
            except Exception as exc:  # noqa: BLE001
                print(f"matrix gex snapshot: compute failed for {symbol}: {exc}")
                continue
            rows.append((
                symbol, session_date, minute_ms, float(spot),
                snap["total_gex"], snap["total_dex"],
                snap["total_vex"], snap["total_chex"],
                snap["flip"], snap["call_wall"], snap["put_wall"],
                snap["max_gamma_strike"], snap["regime"],
                snap["atm_iv"], snap["term_slope"],
                snap["gex_scale"], snap["vex_scale"], snap["chex_scale"],
                updated_ms,
            ))
        try:
            await asyncio.to_thread(_matrix_gex_db_insert, rows)
        except Exception as exc:  # noqa: BLE001
            print(f"matrix gex snapshot: db write failed: {exc}")


# -----------------------------------------------------------------------------
# Regime journal (morning checklist + auto-grading)
# A trader writes a regime call before the open; the system freezes the
# engine's verdict at save time, then grades both calls after the close using
# the SAME outcome rules as tools/backtest_regime.py (matrix_outcome). One
# entry per (session_date, symbol); entries lock once graded.
# -----------------------------------------------------------------------------
# Calls lock shortly after the close (16:05 ET): afterwards "today's call"
# targets the next trading session, and the finished session can be graded.
MATRIX_JOURNAL_CLOSE_MINUTE = 16 * 60 + 5
# Accuracy on fewer scored days than this is noise, not signal (mirrors the
# backtester's SMALL_SAMPLE_DAYS).
MATRIX_JOURNAL_SMALL_SAMPLE_DAYS = 30

_MATRIX_JOURNAL_COLUMNS = (
    "session_date", "symbol", "created_ms", "user_label", "user_levels",
    "user_notes", "engine_label", "engine_agreement", "engine_reasoning",
    "call_wall", "put_wall", "outcome", "grade", "engine_grade", "graded_ms",
)


def _matrix_journal_session_date(now_et: datetime.datetime | None = None) -> datetime.date:
    """The trading session a new call is FOR: today until the close, then the
    next weekday (weekends roll forward to Monday)."""
    now_et = now_et or datetime.datetime.now(ZoneInfo("America/New_York"))
    day = now_et.date()
    if day.weekday() < 5 and now_et.hour * 60 + now_et.minute >= MATRIX_JOURNAL_CLOSE_MINUTE:
        day += datetime.timedelta(days=1)
    while day.weekday() >= 5:
        day += datetime.timedelta(days=1)
    return day


def _matrix_journal_gradeable(session_date: str, now_et: datetime.datetime | None = None) -> bool:
    """A session's outcome is computable once its close has passed."""
    now_et = now_et or datetime.datetime.now(ZoneInfo("America/New_York"))
    day = datetime.date.fromisoformat(session_date)
    if day < now_et.date():
        return True
    return day == now_et.date() and now_et.hour * 60 + now_et.minute >= MATRIX_JOURNAL_CLOSE_MINUTE


def _matrix_journal_row_to_entry(row: tuple[Any, ...]) -> dict[str, Any]:
    record = dict(zip(_MATRIX_JOURNAL_COLUMNS, row))
    reasoning = record.get("engine_reasoning")
    try:
        reasoning = json.loads(reasoning) if reasoning else []
    except ValueError:
        reasoning = [str(reasoning)]
    return {
        "date": record["session_date"],
        "symbol": record["symbol"],
        "created_ms": record["created_ms"],
        "user_label": record["user_label"],
        "user_levels": record.get("user_levels") or "",
        "user_notes": record.get("user_notes") or "",
        "engine_label": record.get("engine_label"),
        "engine_agreement": record.get("engine_agreement"),
        "engine_reasoning": reasoning,
        "call_wall": record.get("call_wall"),
        "put_wall": record.get("put_wall"),
        "outcome": record.get("outcome"),
        "grade": record.get("grade"),
        "engine_grade": record.get("engine_grade"),
        "graded_ms": record.get("graded_ms"),
        "locked": record.get("graded_ms") is not None,
    }


def _matrix_journal_db_get(session_date: str, symbol: str) -> dict[str, Any] | None:
    _matrix_flow_db_init()
    with sqlite3.connect(_matrix_flow_db_path(), timeout=20) as connection:
        row = connection.execute(
            f"SELECT {', '.join(_MATRIX_JOURNAL_COLUMNS)} FROM matrix_journal_entries"
            " WHERE session_date = ? AND symbol = ?",
            (session_date, symbol),
        ).fetchone()
    return _matrix_journal_row_to_entry(row) if row else None


def _matrix_journal_db_upsert(entry: dict[str, Any]) -> dict[str, Any]:
    """Save/replace the (date, symbol) call. Never touches outcome/grade
    fields — callers must check the lock first; grades are write-once."""
    _matrix_flow_db_init()
    values = (
        entry["date"], entry["symbol"], int(entry["created_ms"]), entry["user_label"],
        entry.get("user_levels") or "", entry.get("user_notes") or "",
        entry.get("engine_label"), entry.get("engine_agreement"),
        json.dumps(entry.get("engine_reasoning") or []),
        entry.get("call_wall"), entry.get("put_wall"),
    )
    with sqlite3.connect(_matrix_flow_db_path(), timeout=20) as connection:
        connection.execute("""
            INSERT INTO matrix_journal_entries (
                session_date, symbol, created_ms, user_label, user_levels,
                user_notes, engine_label, engine_agreement, engine_reasoning,
                call_wall, put_wall
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_date, symbol) DO UPDATE SET
                created_ms=excluded.created_ms,
                user_label=excluded.user_label,
                user_levels=excluded.user_levels,
                user_notes=excluded.user_notes,
                engine_label=excluded.engine_label,
                engine_agreement=excluded.engine_agreement,
                engine_reasoning=excluded.engine_reasoning,
                call_wall=excluded.call_wall,
                put_wall=excluded.put_wall
        """, values)
        connection.commit()
    return _matrix_journal_db_get(entry["date"], entry["symbol"])


def _matrix_journal_db_list(days: int = 60, symbol: str | None = None) -> list[dict[str, Any]]:
    _matrix_flow_db_init()
    since = (
        datetime.datetime.now(ZoneInfo("America/New_York")).date()
        - datetime.timedelta(days=max(1, int(days)))
    ).isoformat()
    query = (
        f"SELECT {', '.join(_MATRIX_JOURNAL_COLUMNS)} FROM matrix_journal_entries"
        " WHERE session_date >= ?"
    )
    params: list[Any] = [since]
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    query += " ORDER BY session_date DESC, symbol"
    with sqlite3.connect(_matrix_flow_db_path(), timeout=20) as connection:
        rows = connection.execute(query, params).fetchall()
    return [_matrix_journal_row_to_entry(row) for row in rows]


def _matrix_journal_db_pending(now_et: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """Ungraded entries whose session has completed (auto-grade candidates)."""
    _matrix_flow_db_init()
    with sqlite3.connect(_matrix_flow_db_path(), timeout=20) as connection:
        rows = connection.execute(
            f"SELECT {', '.join(_MATRIX_JOURNAL_COLUMNS)} FROM matrix_journal_entries"
            " WHERE graded_ms IS NULL ORDER BY session_date"
        ).fetchall()
    return [
        entry
        for entry in (_matrix_journal_row_to_entry(row) for row in rows)
        if _matrix_journal_gradeable(entry["date"], now_et)
    ]


def _matrix_journal_db_mark_graded(
    session_date: str,
    symbol: str,
    outcome: str,
    grade: int | None,
    engine_grade: int | None,
    graded_ms: int,
) -> dict[str, Any] | None:
    """Write-once grading: no-op when the entry is already graded."""
    _matrix_flow_db_init()
    with sqlite3.connect(_matrix_flow_db_path(), timeout=20) as connection:
        cursor = connection.execute("""
            UPDATE matrix_journal_entries
            SET outcome = ?, grade = ?, engine_grade = ?, graded_ms = ?
            WHERE session_date = ? AND symbol = ? AND graded_ms IS NULL
        """, (outcome, grade, engine_grade, graded_ms, session_date, symbol))
        connection.commit()
    if cursor.rowcount < 1:
        return None
    return _matrix_journal_db_get(session_date, symbol)


def _matrix_journal_candles_ohlc(candles: list[dict[str, Any]], session_date: str) -> tuple[float, float, float, float] | None:
    """Full-session (open, high, low, close) for one ET date from intraday bars."""
    for day in matrix_regime.daily_ohlc(candles):
        if day["date"] == session_date:
            return (day["open"], day["high"], day["low"], day["close"])
    return None


def _matrix_journal_grade_entry(
    entry: dict[str, Any],
    ohlc: tuple[float, float, float, float] | None,
    graded_ms: int | None = None,
) -> dict[str, Any] | None:
    """Grade one entry from the session's (open, high, low, close).

    Uses the shared matrix_outcome rules (identical to the backtester): the
    user's label AND the engine's frozen label are each mapped through the
    same prediction table, enabling "you vs the engine" stats. MIXED makes no
    prediction: its grade stores NULL and is excluded from hit-rate
    denominators. Returns None when the day cannot be classified.
    """
    if not ohlc:
        return None
    outcome, drift, _wall = matrix_outcome.classify_outcome(
        *ohlc, walls=(entry.get("call_wall"), entry.get("put_wall")))
    user_hit = matrix_outcome.predicts(entry.get("user_label"), outcome, drift)
    engine_label = entry.get("engine_label")
    engine_hit = matrix_outcome.predicts(engine_label, outcome, drift) if engine_label else None
    return {
        "outcome": outcome,
        "drift": drift,
        "grade": None if user_hit is None else int(user_hit),
        "engine_grade": None if engine_hit is None else int(engine_hit),
        "graded_ms": graded_ms if graded_ms is not None else int(time.time() * 1000),
    }


async def _matrix_journal_day_ohlc(symbol: str, session_date: str) -> tuple[float, float, float, float] | None:
    """Full-session OHLC for grading: the candles feed first, then the
    snapshot DB's own spot series (the same fallback the backtester uses)."""
    if symbol in MATRIX_CANDLE_SYMBOLS:
        try:
            payload = await _fetch_matrix_candles(symbol=symbol, interval="60m", range_="1mo")
            ohlc = _matrix_journal_candles_ohlc(payload.get("candles") or [], session_date)
            if ohlc is not None:
                return ohlc
        except Exception:  # noqa: BLE001 — fall through to the spot series
            pass
    path = _matrix_flow_db_path()
    if path.exists():
        with sqlite3.connect(path, timeout=20) as connection:
            spots = [row[0] for row in connection.execute(
                "SELECT spot FROM matrix_gex_snapshot"
                " WHERE symbol = ? AND session_date = ? ORDER BY minute_ms",
                (symbol, session_date),
            )]
        spots = [float(spot) for spot in spots if spot]
        if spots:
            return (spots[0], max(spots), min(spots), spots[-1])
    return None


async def _matrix_journal_auto_grade(
    provider: Any = None,
    now_et: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    """Grade every completed-session entry still awaiting an outcome.

    provider(symbol, session_date) -> (open, high, low, close) | None;
    defaults to the candles feed + snapshot spot series. Returns the newly
    graded entries.
    """
    provider = provider or _matrix_journal_day_ohlc
    graded: list[dict[str, Any]] = []
    for entry in _matrix_journal_db_pending(now_et):
        try:
            ohlc = await provider(entry["symbol"], entry["date"])
        except Exception:  # noqa: BLE001 — leave it for a later pass
            continue
        fields = _matrix_journal_grade_entry(entry, ohlc)
        if fields is None:
            continue
        updated = _matrix_journal_db_mark_graded(
            entry["date"], entry["symbol"], fields["outcome"],
            fields["grade"], fields["engine_grade"], fields["graded_ms"])
        if updated is not None:
            graded.append(updated)
    return graded


def _matrix_journal_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Hit rates: user overall, engine overall, and the agree/disagree split
    that answers "should I trust myself or the machine?".

    Only graded entries with a non-NULL grade are scored (MIXED makes no
    prediction and is excluded from every denominator, like the backtester).
    """
    def tally(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
        scored = [row for row in rows if row.get(key) is not None]
        hits = sum(int(row[key]) for row in scored)
        return {"scored": len(scored), "hits": hits,
                "hit_rate": (hits / len(scored)) if scored else None}

    graded = [entry for entry in entries if entry.get("graded_ms") is not None]
    user_scored = [entry for entry in graded if entry.get("grade") is not None]
    agreeing = [entry for entry in user_scored
                if entry.get("engine_label") and entry["user_label"] == entry["engine_label"]]
    disagreeing = [entry for entry in user_scored
                   if entry.get("engine_label") and entry["user_label"] != entry["engine_label"]]
    by_label: dict[str, dict[str, Any]] = {}
    for who, grade_key, label_key in (
        ("user", "grade", "user_label"),
        ("engine", "engine_grade", "engine_label"),
    ):
        by_label[who] = {
            label: tally([e for e in graded if e.get(label_key) == label], grade_key)
            for label in matrix_regime.LABELS
        }
    user = tally(graded, "grade")
    small_sample = user["scored"] < MATRIX_JOURNAL_SMALL_SAMPLE_DAYS
    return {
        "ok": True,
        "entries": len(entries),
        "graded": len(graded),
        "user": user,
        "engine": tally(graded, "engine_grade"),
        "user_when_agreeing": tally(agreeing, "grade"),
        "user_when_disagreeing": tally(disagreeing, "grade"),
        "by_label": by_label,
        "small_sample_threshold": MATRIX_JOURNAL_SMALL_SAMPLE_DAYS,
        "small_sample": small_sample,
        "caution": (
            f"Fewer than {MATRIX_JOURNAL_SMALL_SAMPLE_DAYS} scored days — "
            "small-sample noise dominates; do not draw conclusions yet."
        ) if small_sample else None,
    }


async def _compute_matrix_regime(symbol: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Engine verdict plus the GEX snapshot it was computed from.

    Shared by /api/matrix/regime and the journal save path (which freezes
    this verdict alongside the user's call). Uses the already-cached chain
    plus the existing candles feed aggregated to daily bars — no extra
    upstream calls beyond what those caches already make. Raises on
    unavailable data; callers decide how to degrade.
    """
    data = await _fetch_matrix_market_data()
    record = data.get(symbol)
    if not isinstance(record, dict):
        raise ValueError(f"Unsupported regime symbol {symbol!r}")
    collector = _matrix_flow_collector
    spot = None
    if collector is not None:
        spot = collector.latest_spot.get(symbol)
    if not spot and symbol == "VIX":
        vix = await _fetch_matrix_cboe_spot_cached("VIX")
        spot = vix["spot"] if vix else None
    if not spot:
        spot = record.get("spot")
    if not spot:
        raise ValueError(f"No spot available for {symbol}")
    updated_ms = int(time.time() * 1000)
    snap = _compute_matrix_gex_snapshot(symbol, record, float(spot), updated_ms)
    candles: list[dict[str, Any]] = []
    if symbol in MATRIX_CANDLE_SYMBOLS:
        try:
            payload = await _fetch_matrix_candles(symbol=symbol, interval="60m", range_="1mo")
            candles = matrix_regime.daily_ohlc(payload.get("candles") or [])
        except Exception:  # noqa: BLE001 — VRP leg degrades to "unavailable"
            candles = []
    verdict = matrix_regime.compute_regime({
        **snap,
        "options": record.get("opts") or [],
        "candles": candles,
        "valuation_ms": updated_ms,
    })
    return verdict, snap


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
        self._matrix_flow_task: asyncio.Task[Any] | None = None
        self._matrix_gex_task: asyncio.Task[Any] | None = None
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
            await self._response(send, 200, LANDING_PAGE.encode("utf-8"), b"text/html; charset=utf-8")
            return
        if method == "GET" and path == "/health":
            await self._json(send, {"status": "ok", "mcp_path": self.mcp_path, "connectors": sorted(self.connectors), "create_path": "/create", "auth": "oauth" if self.oauth else "bearer"})
            return
        if method == "GET" and path == "/create":
            await self._response(send, 200, CREATE_PAGE.encode("utf-8"), b"text/html; charset=utf-8")
            return
        if method == "GET" and path == "/matrix":
            await self._response(
                send,
                307,
                b"",
                b"text/plain; charset=utf-8",
                headers=[(b"location", b"/matrix/")],
            )
            return
        matrix_static = {
            "/matrix/": ("matrix.html", b"text/html; charset=utf-8"),
            "/matrix/matrix.css": ("matrix.css", b"text/css; charset=utf-8"),
            "/matrix/matrix.js": ("matrix.js", b"text/javascript; charset=utf-8"),
        }
        if method == "GET" and path in matrix_static:
            filename, content_type = matrix_static[path]
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
        if path == "/api/matrix/symbol-search":
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
                await self._matrix_symbol_search(scope, send)
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
        if path == "/api/matrix/active-symbol":
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
                await self._matrix_active_symbol(scope, send)
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
        if path == "/api/matrix/flow":
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
                await self._matrix_flow(scope, send)
                return
        if path == "/api/matrix/spots":
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
                await self._matrix_spots(scope, send)
                return
        if path == "/api/matrix/gex-history":
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
                await self._matrix_gex_history(scope, send)
                return
        if path == "/api/matrix/regime":
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
                await self._matrix_regime(scope, send)
                return
        if path == "/api/matrix/journal":
            if method == "OPTIONS":
                await self._response(
                    send,
                    204,
                    b"",
                    b"text/plain; charset=utf-8",
                    headers=self._matrix_cors_headers(scope),
                )
                return
            if method == "POST":
                await self._matrix_journal_save(scope, receive, send)
                return
            if method == "GET":
                await self._matrix_journal_list(scope, send)
                return
        if path == "/api/matrix/journal/grade":
            if method == "OPTIONS":
                await self._response(
                    send,
                    204,
                    b"",
                    b"text/plain; charset=utf-8",
                    headers=self._matrix_cors_headers(scope),
                )
                return
            if method == "POST":
                await self._matrix_journal_grade(scope, receive, send)
                return
        if path == "/api/matrix/journal/stats":
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
                await self._matrix_journal_stats_endpoint(scope, send)
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
                    await self._start_matrix_flow_collector()
                    await self._start_matrix_gex_snapshotter()
                    self._started = True
                    await send({"type": "lifespan.startup.complete"})
                except BaseException as exc:  # noqa: BLE001
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
            elif message["type"] == "lifespan.shutdown":
                if self._matrix_flow_task is not None:
                    self._matrix_flow_task.cancel()
                    await asyncio.gather(self._matrix_flow_task, return_exceptions=True)
                if self._matrix_gex_task is not None:
                    self._matrix_gex_task.cancel()
                    await asyncio.gather(self._matrix_gex_task, return_exceptions=True)
                if self._stack is not None:
                    await self._stack.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _start_matrix_flow_collector(self) -> None:
        global _matrix_flow_collector
        api_key = _matrix_lse_api_key()
        enabled = os.getenv("TRIPITY_MATRIX_FLOW_COLLECTOR", "1").strip().lower() not in {"0", "false", "off", "no"}
        if not api_key or not enabled or self._matrix_flow_task is not None:
            return
        _matrix_flow_collector = MatrixFlowCollector(api_key)
        self._matrix_flow_task = asyncio.create_task(
            _matrix_flow_collector.run(),
            name="matrix-lse-options-flow-collector",
        )

    async def _start_matrix_gex_snapshotter(self) -> None:
        enabled = os.getenv("TRIPITY_MATRIX_GEX_SNAPSHOTS", "1").strip().lower() not in {"0", "false", "off", "no"}
        if not enabled or self._matrix_gex_task is not None:
            return
        self._matrix_gex_task = asyncio.create_task(
            _matrix_gex_snapshot_loop(),
            name="matrix-gex-snapshotter",
        )

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
            await self._json(
                send,
                data,
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"public, max-age=30"),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_symbol_search(self, scope: dict[str, Any], send: Any) -> None:
        try:
            query = parse_qs(scope.get("query_string", b"").decode("latin1"))
            text = (query.get("q") or [""])[0]
            items = await _search_matrix_symbols(text)
            await self._json(
                send,
                {"ok": True, "query": text, "items": items},
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"public, max-age=300"),
                ],
            )
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
            collector = _matrix_flow_collector
            if collector is not None:
                collector.set_active_symbol(symbol)
            await self._json(
                send,
                {
                    "ok": True,
                    "symbol": symbol,
                    "record": record,
                    "cache_scope": "et_calendar_day",
                    "spot_source": "lse_websocket" if collector is not None else "cboe_delayed",
                    "history_available": False,
                    "flow_available": False,
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

    async def _matrix_active_symbol(self, scope: dict[str, Any], send: Any) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin1"))
        symbol = (query.get("symbol") or [""])[0].upper().strip()
        collector = _matrix_flow_collector
        if collector is not None and symbol in MATRIX_LSE_PRICE_SYMBOLS:
            collector.clear_active_symbol()
        await self._json(
            send,
            {
                "ok": True,
                "symbol": symbol or None,
                "dynamic_symbol": collector.dynamic_symbol if collector is not None else None,
            },
            headers=[
                *self._matrix_cors_headers(scope),
                (b"cache-control", b"no-store"),
            ],
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
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_flow(self, scope: dict[str, Any], send: Any) -> None:
        try:
            query = parse_qs(scope.get("query_string", b"").decode("latin1"))
            expiration_values = query.get("expiration") or []
            expirations = tuple(
                item.strip()
                for value in expiration_values
                for item in value.split(",")
                if item.strip()
            )
            symbol = (query.get("symbol") or ["SPY"])[0]
            interval = (query.get("interval") or ["1m"])[0]
            session_offset = int((query.get("session_offset") or ["0"])[0])
            requested_session = (query.get("session_date") or [""])[0].strip()
            latest_session = _matrix_session_date().isoformat()
            if requested_session and requested_session != latest_session:
                data = await _fetch_matrix_flow_history(symbol, interval, expirations, requested_session)
            elif session_offset:
                data = await _fetch_matrix_flow_history(symbol, interval, expirations, session_offset)
            else:
                data = await _fetch_matrix_flow(symbol=symbol, interval=interval, expirations=expirations)
            data["session_date"] = requested_session or _matrix_session_date(session_offset).isoformat()
            data["available_sessions"] = list(_matrix_retained_session_dates())
            await self._json(
                send,
                data,
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"public, max-age=10"),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_spots(self, scope: dict[str, Any], send: Any) -> None:
        """Latest spot per symbol: WebSocket ticks when available, else the
        50s-cached LSE 1-minute REST spot. No additional upstream calls."""
        try:
            symbols: dict[str, Any] = {}
            collector = _matrix_flow_collector
            if collector is not None:
                for symbol, spot in collector.latest_spot.items():
                    symbols[symbol] = {
                        "spot": float(spot),
                        "asof": collector.latest_spot_asof.get(symbol) or collector.last_tick,
                        "source": "lse_websocket",
                    }
            missing = [symbol for symbol in MATRIX_LSE_PRICE_SYMBOLS if symbol not in symbols]
            if missing:
                try:
                    rest_spots = await _fetch_matrix_lse_spots()
                except Exception:  # noqa: BLE001
                    rest_spots = {}
                for symbol in missing:
                    entry = rest_spots.get(symbol)
                    if entry:
                        symbols[symbol] = {
                            "spot": entry["spot"],
                            "asof": entry.get("asof"),
                            "source": "lse_rest_1m",
                        }
            # VIX has no LSE stream; use the cached CBOE delayed quote instead.
            if "VIX" not in symbols:
                vix_spot = await _fetch_matrix_cboe_spot_cached("VIX")
                if vix_spot:
                    symbols["VIX"] = vix_spot
            await self._json(
                send,
                {
                    "ok": True,
                    "asof": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "symbols": symbols,
                },
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", MATRIX_SPOTS_CACHE_CONTROL),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_gex_history(self, scope: dict[str, Any], send: Any) -> None:
        try:
            query = parse_qs(scope.get("query_string", b"").decode("latin1"))
            symbol = (query.get("symbol") or ["SPY"])[0]
            session_offset = int((query.get("session_offset") or ["0"])[0])
            requested_session = (query.get("session_date") or [""])[0].strip()
            if requested_session:
                data = await _fetch_matrix_gex_history(symbol, requested_session)
            elif session_offset:
                data = await _fetch_matrix_gex_history(symbol, session_offset)
            else:
                # Default to the most recent completed session.
                data = await _fetch_matrix_gex_history(symbol, 1)
            await self._json(
                send,
                data,
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"public, max-age=30"),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_regime(self, scope: dict[str, Any], send: Any) -> None:
        """Deterministic regime verdict (matrix_regime.compute_regime) for one
        core symbol — see _compute_matrix_regime for the data path."""
        try:
            query = parse_qs(scope.get("query_string", b"").decode("latin1"))
            symbol = (query.get("symbol") or ["SPY"])[0].upper()
            verdict, _snap = await _compute_matrix_regime(symbol)
            verdict.update({
                "ok": True,
                "symbol": symbol,
                "asof": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            await self._json(
                send,
                verdict,
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"public, max-age=30"),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_journal_save(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Save/replace today's pre-open call, freezing the engine's current
        verdict (label/agreement/reasoning + walls) alongside it so the
        user-vs-engine comparison is fixed at decision time. Entries lock
        once graded; until then a re-save replaces the call."""
        try:
            body = await self._read_body(receive)
            payload = json.loads(body.decode("utf-8") or "{}")
            symbol = str(payload.get("symbol") or "").upper().strip()
            if symbol not in MATRIX_LSE_PRICE_SYMBOLS:
                raise ValueError(f"Unsupported journal symbol {symbol!r}")
            user_label = str(payload.get("user_label") or payload.get("label") or "").upper().strip()
            if user_label not in matrix_regime.LABELS:
                raise ValueError(f"user_label must be one of {', '.join(matrix_regime.LABELS)}")
            session_date = _matrix_journal_session_date().isoformat()
            existing = await asyncio.to_thread(_matrix_journal_db_get, session_date, symbol)
            if existing and existing.get("locked"):
                await self._json(
                    send,
                    {"ok": False, "detail": "journal entry is locked (already graded)", "entry": existing},
                    status=409,
                    headers=self._matrix_cors_headers(scope),
                )
                return
            engine: dict[str, Any] = {}
            try:
                verdict, snap = await _compute_matrix_regime(symbol)
                engine = {
                    "engine_label": verdict.get("label"),
                    "engine_agreement": verdict.get("agreement"),
                    "engine_reasoning": verdict.get("reasoning") or [],
                    "call_wall": snap.get("call_wall"),
                    "put_wall": snap.get("put_wall"),
                }
            except Exception:  # noqa: BLE001 — the user's call still saves
                engine = {}
            entry = {
                "date": session_date,
                "symbol": symbol,
                "created_ms": int(time.time() * 1000),
                "user_label": user_label,
                "user_levels": str(payload.get("user_levels") or payload.get("levels") or "").strip(),
                "user_notes": str(payload.get("user_notes") or payload.get("notes") or "").strip(),
                "engine_label": engine.get("engine_label"),
                "engine_agreement": engine.get("engine_agreement"),
                "engine_reasoning": engine.get("engine_reasoning") or [],
                "call_wall": engine.get("call_wall"),
                "put_wall": engine.get("put_wall"),
            }
            saved = await asyncio.to_thread(_matrix_journal_db_upsert, entry)
            await self._json(
                send,
                {"ok": True, "entry": saved, "engine_frozen": bool(engine)},
                status=201,
                headers=self._matrix_cors_headers(scope),
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

    async def _matrix_journal_list(self, scope: dict[str, Any], send: Any) -> None:
        """Recent entries with grades. Past ungraded days are auto-graded on
        the way out (best effort — listing never fails because grading did)."""
        try:
            query = parse_qs(scope.get("query_string", b"").decode("latin1"))
            days = int((query.get("days") or ["60"])[0])
            symbol = (query.get("symbol") or [""])[0].upper().strip() or None
            try:
                graded = await _matrix_journal_auto_grade()
            except Exception:  # noqa: BLE001
                graded = []
            entries = await asyncio.to_thread(_matrix_journal_db_list, days, symbol)
            await self._json(
                send,
                {
                    "ok": True,
                    "days": days,
                    "session_date": _matrix_journal_session_date().isoformat(),
                    "labels": list(matrix_regime.LABELS),
                    "entries": entries,
                    "newly_graded": len(graded),
                },
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"no-store"),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            await self._json(
                send,
                {"ok": False, "detail": str(exc)},
                status=502,
                headers=self._matrix_cors_headers(scope),
            )

    async def _matrix_journal_grade(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Grade an entry (or all entries for a date) against the session's
        realized outcome, using the shared matrix_outcome rules."""
        try:
            body = await self._read_body(receive)
            payload = json.loads(body.decode("utf-8") or "{}")
            session_date = str(payload.get("date") or "").strip()
            try:
                datetime.date.fromisoformat(session_date)
            except ValueError:
                raise ValueError("date must use YYYY-MM-DD") from None
            symbol = str(payload.get("symbol") or "").upper().strip() or None
            if not _matrix_journal_gradeable(session_date):
                raise ValueError(f"session {session_date} is not complete yet")
            pending = [
                entry for entry in await asyncio.to_thread(_matrix_journal_db_pending)
                if entry["date"] == session_date and (symbol is None or entry["symbol"] == symbol)
            ]
            if not pending:
                existing = await asyncio.to_thread(_matrix_journal_db_get, session_date, symbol) if symbol else None
                if existing and existing.get("locked"):
                    await self._json(
                        send,
                        {"ok": True, "graded": [], "entry": existing, "detail": "already graded"},
                        headers=self._matrix_cors_headers(scope),
                    )
                else:
                    await self._json(
                        send,
                        {"ok": False, "detail": "no journal entry to grade for that date/symbol"},
                        status=404,
                        headers=self._matrix_cors_headers(scope),
                    )
                return
            graded: list[dict[str, Any]] = []
            for entry in pending:
                ohlc = await _matrix_journal_day_ohlc(entry["symbol"], entry["date"])
                fields = _matrix_journal_grade_entry(entry, ohlc)
                if fields is None:
                    continue
                updated = await asyncio.to_thread(
                    _matrix_journal_db_mark_graded,
                    entry["date"], entry["symbol"], fields["outcome"],
                    fields["grade"], fields["engine_grade"], fields["graded_ms"])
                if updated is not None:
                    graded.append(updated)
            await self._json(
                send,
                {"ok": True, "graded": graded, "graded_count": len(graded)},
                headers=self._matrix_cors_headers(scope),
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

    async def _matrix_journal_stats_endpoint(self, scope: dict[str, Any], send: Any) -> None:
        """Hit rates: user overall, engine overall, user-when-agreeing vs
        user-when-disagreeing, plus a per-label breakdown and a small-sample
        caution. Past ungraded days are auto-graded first (best effort)."""
        try:
            query = parse_qs(scope.get("query_string", b"").decode("latin1"))
            days = int((query.get("days") or ["60"])[0])
            symbol = (query.get("symbol") or [""])[0].upper().strip() or None
            try:
                await _matrix_journal_auto_grade()
            except Exception:  # noqa: BLE001
                pass
            entries = await asyncio.to_thread(_matrix_journal_db_list, days, symbol)
            stats = _matrix_journal_stats(entries)
            stats.update({"days": days, "symbol": symbol})
            await self._json(
                send,
                stats,
                headers=[
                    *self._matrix_cors_headers(scope),
                    (b"cache-control", b"no-store"),
                ],
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
