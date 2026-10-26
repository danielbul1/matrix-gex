"""Key Levels screen (build 4, #34): price chart with the four Key Levels and the
Forecast under it, for any ticker the screen can search."""
import asyncio
import re

import pytest

import test_matrix_routes as routes

host = routes.host


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        _FakeClient.calls.append(url)
        return _FakeResponse({"chart": {"result": [{
            "timestamp": [1792785540, 1792785600],
            "indicators": {"quote": [{
                "open": [10.0, 10.5], "high": [11.0, 11.0], "low": [9.5, 10.0], "close": [10.5, 10.8],
            }]},
        }]}})


@pytest.fixture
def yahoo(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(host.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(host, "_matrix_lse_api_key", lambda: None)
    host._matrix_candles_cache.clear()
    yield _FakeClient
    host._matrix_candles_cache.clear()


@pytest.mark.parametrize("symbol, yahoo_symbol", [("AAPL", "AAPL"), ("VIX", "^VIX"), ("SPX", "^GSPC")])
def test_candles_load_for_any_ticker(yahoo, symbol, yahoo_symbol):
    data = asyncio.run(host._fetch_matrix_candles(symbol=symbol.lower(), interval="5m", range_="5d"))
    assert data["ok"] and data["symbol"] == symbol
    assert [c["close"] for c in data["candles"]] == [10.5, 10.8]
    assert yahoo.calls[-1].endswith("/" + yahoo_symbol)


@pytest.mark.parametrize("symbol", ["", "1ABC", "TOOLONGX", "A B"])
def test_candles_reject_invalid_ticker(yahoo, symbol):
    with pytest.raises(ValueError):
        asyncio.run(host._fetch_matrix_candles(symbol=symbol, interval="5m", range_="5d"))
    assert yahoo.calls == []


def test_candle_cache_stays_bounded(yahoo, monkeypatch):
    monkeypatch.setattr(host, "MATRIX_CANDLES_CACHE_MAX", 3)
    for symbol in ("AA", "BB", "CC", "DD", "EE"):
        asyncio.run(host._fetch_matrix_candles(symbol=symbol, interval="5m", range_="5d"))
    assert len(host._matrix_candles_cache) <= 3


def _key_levels_section():
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    match = re.search(r'<section class="app-view" id="view-key-levels".*?</section>', page, re.S)
    assert match
    return match.group(0)


def test_key_levels_screen_has_chart_and_forecast():
    section = _key_levels_section()
    assert 'id="keyLevelsChart"' in section
    assert 'id="forecast"' in section
    assert section.index('id="keyLevelsChart"') < section.index('id="forecast"')
    assert "Coming soon" not in section
    # Ticker and Search only: no expiry choice on this screen.
    assert 'data-screen="key-levels"' in section
    assert "expir" not in section.lower()


def test_no_max_pain_anywhere():
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    assert "max pain" not in page.lower()
    assert "max pain" not in script.lower()
    assert "maxpain" not in script.lower()


def test_key_levels_chart_has_no_price_line():
    """Only the four levels cross the chart: no current-price line or "Price" label."""
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    start = script.index("function drawKeyLevelsChart")
    body = script[start:script.index("\nfunction ", start + 1)]
    assert "R.spot" in body  # the price still sets the y range
    assert "y(R.spot)" not in body
    assert "Price ${" not in body
