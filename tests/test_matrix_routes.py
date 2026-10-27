"""Matrix keeps only the screens Quant Power has (ADR 0001).

The page still loads, and the endpoints that served deleted screens (flow,
replay, live spot polling, regime, journal) answer 404. Starting the app
removes the old SQLite store those screens wrote to.
"""
import asyncio
import re
import importlib.util
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "railway-service" / "src"))


def _install_third_party_stubs():
    """public_company_host imports FastAPI/uvicorn/etc. at module load; those
    are not installed in every dev environment, so stub them (import-only)."""
    class _Any:
        def __init__(self, *a, **k): pass
        def __call__(self, *a, **k): return _Any()
        def __getattr__(self, name): return _Any()
        def __mro_entries__(self, bases): return (object,)

    for name in (
        "uvicorn", "mcp", "lse_data",
        "fastapi", "fastapi.responses",
        "fastmcp", "fastmcp.client", "fastmcp.client.auth",
        "fastmcp.server", "fastmcp.server.providers", "fastmcp.server.providers.openapi",
        "fastmcp.utilities", "fastmcp.utilities.openapi",
    ):
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue
        except (ModuleNotFoundError, ValueError, TypeError):
            pass
        module = types.ModuleType(name)
        module.__getattr__ = lambda attr: _Any
        sys.modules[name] = module


_install_third_party_stubs()
from tripity_experiment import public_company_host as host  # noqa: E402


def _make_app():
    connector = types.SimpleNamespace(
        slug="t", company_name="t", openapi_url="", api_base_url="",
        mcp_path="/mcp/t", enabled_tools=(), disabled_tools=(), app=None)
    return host.PublicCompanyApp(connector, oauth=False, public_url="http://test")


def _get(app, path, query=b"", headers=()):
    scope = {"type": "http", "method": "GET", "path": path, "query_string": query, "headers": list(headers)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    _get.headers = dict(start.get("headers", []))
    return start["status"], body


def test_matrix_page_still_served():
    status, body = _get(_make_app(), "/matrix/")
    assert status == 200
    assert b'id="gexChart"' in body


@pytest.mark.parametrize("path", [
    "/api/matrix/flow",
    "/api/matrix/spots",
    "/api/matrix/gex-history",
    "/api/matrix/regime",
    "/api/matrix/active-symbol",
    "/api/matrix/journal",
    "/api/matrix/journal/stats",
    "/api/matrix/symbol-search",
])
def test_deleted_endpoints_are_gone(path):
    status, _ = _get(_make_app(), path)
    assert status == 404


def test_page_has_no_deleted_screens():
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    for view in ("view-dex", "view-journal", "view-net-flow", "view-max-pain", "gexReplayBar", "autorefresh"):
        assert view not in page
    for endpoint in ("/api/matrix/flow", "/api/matrix/spots", "/api/matrix/journal", "/api/matrix/regime"):
        assert endpoint not in script


def test_startup_removes_legacy_store(tmp_path, monkeypatch):
    store = tmp_path / "matrix_flow.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"matrix_flow.sqlite3{suffix}").write_bytes(b"old")
    monkeypatch.setenv("TRIPITY_MATRIX_FLOW_DB", str(store))
    host._remove_legacy_matrix_store()
    assert list(tmp_path.iterdir()) == []
    host._remove_legacy_matrix_store()  # already gone: no error


SCREEN_PATHS = [
    "/matrix/",
    "/matrix/oi-volume",
    "/matrix/gex-analysis",
    "/matrix/key-levels",
    "/matrix/options-summary",
    "/matrix/how-to-use-gex",
    "/matrix/disclaimer",
]


@pytest.mark.parametrize("path", SCREEN_PATHS + ["/matrix/key-levels/"])
def test_every_screen_path_serves_the_page(path):
    """Each screen opens directly, including on a browser reload."""
    status, body = _get(_make_app(), path)
    assert status == 200
    assert body == (host.MATRIX_WEB_DIR / "matrix.html").read_bytes()


def test_unknown_matrix_path_is_404():
    status, _ = _get(_make_app(), "/matrix/journal")
    assert status == 404


def test_bare_matrix_redirect_keeps_the_ticker():
    status, _ = _get(_make_app(), "/matrix", query=b"ticker=SPX")
    assert status == 307
    assert _get.headers[b"location"] == b"/matrix/?ticker=SPX"


def test_menu_follows_his_order():
    """Home is the brand link; the menu then lists his screens in his order,
    with Disclaimer last as its own link."""
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    links = re.findall(r'<a [^>]*href="(/matrix/[^"]*)"[^>]*>([^<]+)</a>', page)
    assert links == [
        ("/matrix/", "Matrix"),
        ("/matrix/oi-volume", "OI Volume"),
        ("/matrix/gex-analysis", "GEX Analysis"),
        ("/matrix/key-levels", "Key Levels"),
        ("/matrix/options-summary", "P/C Ratio"),
        ("/matrix/how-to-use-gex", "How to use GEX"),
        ("/matrix/disclaimer", "Disclaimer"),
    ]


def test_assets_load_from_any_screen_path():
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    assert 'href="/matrix/matrix.css' in page
    assert 'src="/matrix/matrix.js' in page


def test_old_shell_is_gone():
    """The per-screen ticker field replaces Data Health, favorites and the search list,
    and nothing reloads on a timer."""
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    for old in ("dataHealth", "symbolFavorite", "symbolSearchResults", "staleDataBanner"):
        assert old not in page
        assert old not in script
    assert "setInterval" not in script
    assert "symbol-search" not in script


def test_every_screen_picks_from_three_ticker_buttons():
    """Like his GEX board, every ticker screen has SPX/SPY/QQQ buttons and no
    search box (owner, 2026-10-27)."""
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    assert "<input" not in page and ">Search<" not in page and "ticker-form" not in page
    for view in ("gex", "oi-volume", "key-levels"):
        assert f'<div class="ticker-picker" data-screen="{view}">' in page
    assert "symbol-data" not in script and "loadDynamicSymbol" not in script
    assert "const DEFAULT_SYMBOL = 'SPX';" in script


def test_oi_volume_screen_is_built():
    """OI Volume has his controls: ticker, one expiration date, and his five
    buttons in his order with Volume Spread first and on (map #21, #35)."""
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    section = page.split('id="view-oi-volume"', 1)[1].split("</section>", 1)[0]
    assert "Coming soon" not in section
    assert 'data-screen="oi-volume"' in section
    assert '<select id="oivExpiry"' in section
    buttons = re.findall(r'<button class="pbtn( active)?"\s+data-oiv-metric="([a-z_]+)"[^>]*>([^<]+)</button>', section)
    assert [(label, bool(on)) for on, _, label in buttons] == [
        ("Volume Spread", True),
        ("Call OI", False),
        ("Put OI", False),
        ("Call Vol", False),
        ("Put Vol", False),
    ]
    assert 'id="oiVolumeChart"' in section
    assert 'id="oiVolumePriceChart"' in section


def test_gex_board_is_built_like_his():
    """Build 6 (#36): his GEX board has no ticker box, only ticker and expiry
    buttons with Select all / Clear all, a spot line, the strike table and three
    charts, each with a full-screen button."""
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    start = page.index('id="view-gex-analysis"')
    section = page[start:page.index("</section>", start)]
    assert "ticker-form" not in section
    assert "Coming soon" not in section
    for element in ('id="gbTickers"', 'id="gbExpiries"', 'id="gbSelectAll"', 'id="gbClearAll"',
                    'id="gbSpot"', 'id="gbTable"', 'id="gbTotalChart"', 'id="gbSplitChart"', 'id="gbPriceChart"'):
        assert element in section
    assert section.count('class="gb-expand"') == 3
    assert "Select Ticker:" in section and "Options Chain Board" in section


def test_gex_board_like_his_screenshot():
    """His board: SPX, SPY and QQQ; his column labels; a dot on each column's
    largest cell with its row shaded; Gamma Split by calls and puts with an Abs GEX
    line; and expiry buttons that show each date's share of the gamma."""
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    assert "const SCREEN_TICKERS = ['SPX','SPY','QQQ'];" in script
    assert "const GB_TICKERS = SCREEN_TICKERS;" in script
    labels = re.findall(r"\{key:'\w+',\s+label:'([^']+)'", script[script.index("const GB_COLUMNS"):])[:11]
    assert labels == ["Strike", "Net GEX", "Abs GEX", "Notional Delta", "Total OI", "Call OI", "Put OI",
                      "Total Vol", "cVol", "pVol", "%"]
    assert "gb-max-row" in script and "gb-dot" in script
    assert "gb-key-row" not in script and "gb-spot-row" not in script
    assert "key==='netGex' ? Math.abs(v) : v" in script
    bars = script[script.index("function drawGexBoardBars("):script.index("function drawGexBoardPrice(")]
    assert "s.callGex, GB_CALL_COLOR" in bars and "s.putGex, GB_PUT_COLOR" in bars
    assert "byExpiry" not in script.split("// ---------- GEX Board", 1)[1]
    assert "Gamma Split - Total: " in script and "Net Gamma: " in script
    assert "function gexBoardExpiryShares(" in script and "share.toFixed(2)" in script


@pytest.mark.parametrize("view, title", [("how-to-use-gex", "How to use GEX"), ("disclaimer", "Disclaimer")])
def test_guide_pages_have_content(view, title):
    """Build 8 (#38): both pages carry our own text, laid out like his guide page."""
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    start = page.index(f'id="view-{view}"')
    section = page[start:page.index("</section>", start)]
    assert "Coming soon" not in section
    assert f"<h1>{title}</h1>" in section
    assert "for learning only" in page
    # Our own wording: his login page line must not be copied (map #21).
    assert "educational purposes only" not in page


def test_guide_has_his_structure():
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    start = page.index('id="view-how-to-use-gex"')
    section = page[start:page.index("</section>", start)]
    for tag in ("<h2>", "<h3>", "<ul>", "<table>", "<strong>"):
        assert tag in section


def test_power_zone_is_the_new_definition_everywhere():
    """Build 9 (#39): Power Zone = AG/8 + volume x S^2/20000, near spot only; the
    old score, AVG Power Zone and Power Range exist nowhere."""
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    assert "s.ag/8 + s.totalVol*spot*spot/20000" in script
    assert "0.58*agScore" not in script
    for gone in ("AVG Power Zone", "avgPowerZone", "Power Range", "power-range"):
        assert gone not in script
        assert gone not in page


def test_chain_data_is_gzipped_when_the_browser_accepts_it(monkeypatch):
    """The 4 MB chain download goes out compressed; clients without gzip get plain JSON."""
    import gzip
    import json

    data = {"SPX": {"spot": 6000.0, "opts": [{"k": 6000.0}] * 50}}

    async def fake_market_data():
        return data

    monkeypatch.setattr(host, "_fetch_matrix_market_data", fake_market_data)
    status, body = _get(_make_app(), "/api/matrix/cboe-data", headers=[(b"accept-encoding", b"gzip, br")])
    assert status == 200
    assert _get.headers[b"content-encoding"] == b"gzip"
    assert json.loads(gzip.decompress(body)) == data
    status, body = _get(_make_app(), "/api/matrix/cboe-data")
    assert status == 200 and b"content-encoding" not in _get.headers
    assert json.loads(body) == data


def test_spots_and_chains_load_together(monkeypatch):
    """Live spots no longer wait for the chains: both calls run at once."""
    running = []

    async def chains():
        running.append("chains")
        await asyncio.sleep(0.05)
        assert "spots" in running
        return {"SPY": {"spot": 1.0}}

    async def spots():
        running.append("spots")
        await asyncio.sleep(0.05)
        return {"SPY": {"spot": 2.0, "asof": "2026-10-27T14:00:00Z"}}

    monkeypatch.setattr(host, "_fetch_matrix_market_data_cached", chains)
    monkeypatch.setattr(host, "_fetch_matrix_lse_spots", spots)
    data = asyncio.run(host._fetch_matrix_market_data())
    assert data["SPY"]["spot"] == 2.0
