"""The Strike Chart (home screen) works like his: one expiration from a
dropdown, seven parameter buttons, and two price charts of the searched
ticker that load with it. Any ticker that can be searched gets candles."""
import asyncio
import re

import pytest

from test_matrix_routes import _get, _make_app, host


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        _FakeClient.calls.append(url)
        return _FakeResponse({"chart": {"result": [{
            "timestamp": [1793025000, 1793025060],
            "indicators": {"quote": [{
                "open": [10.0, 10.5], "high": [11.0, 11.0], "low": [9.5, 10.0], "close": [10.5, 10.8],
            }]},
        }]}})


@pytest.fixture
def fake_yahoo(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(host.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(host, "_matrix_lse_api_key", lambda: None)
    host._matrix_candles_cache.clear()
    yield _FakeClient.calls
    host._matrix_candles_cache.clear()


@pytest.mark.parametrize("symbol, yahoo", [("AAPL", "AAPL"), ("VIX", "^VIX"), ("SPX", "^GSPC")])
def test_any_searchable_ticker_gets_candles(fake_yahoo, symbol, yahoo):
    data = asyncio.run(host._fetch_matrix_candles(symbol=symbol.lower(), interval="1m", range_="1d"))
    assert data["ok"] is True
    assert data["symbol"] == symbol
    assert [c["close"] for c in data["candles"]] == [10.5, 10.8]
    assert fake_yahoo == [host.MATRIX_YAHOO_CHART_URL.format(yahoo)]


def test_bad_ticker_is_400():
    status, body = _get(_make_app(), "/api/matrix/candles", query=b"symbol=%24%24%24")
    assert status == 400
    assert b'"ok": false' in body or b'"ok":false' in body


def _home_section():
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    start = page.index('id="view-gex"')
    return page[start:page.index("</section>", start)]


def test_one_expiration_from_a_dropdown():
    home = _home_section()
    assert '<select id="expirationSelect"' in home
    assert 'type="checkbox"' not in home
    assert "expirationPicker" not in home
    assert "data-expquick" not in home


def test_his_seven_buttons_in_his_order():
    labels = re.findall(r'<button class="pbtn[^"]*"[^>]*>([^<]+)</button>', _home_section())
    assert labels == ["Net GEX", "AG", "Call OI", "Put OI", "Call Vol", "Put Vol", "Power Zone"]


def test_two_price_charts_beside_the_strike_chart():
    home = _home_section()
    assert 'id="gexChart"' in home
    assert 'id="priceChart"' in home
    assert 'id="priceSimpleChart"' in home


def test_expiration_change_redraws_only_the_strike_chart():
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    start = script.index("bind('expirationSelect','change'")
    handler = script[start:script.index("});", start)]
    assert "run()" in handler
    assert "Price" not in handler and "refreshHome" not in handler
