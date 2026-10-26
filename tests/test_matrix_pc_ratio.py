"""P/C Ratio screen (#37): one row per core ticker, put over call across every
expiry, by open interest and by volume. The totals come from the server and
count the whole chain, not just the Strike Chart's strike range."""
import datetime
import re

from test_matrix_routes import _get, _make_app, host


def _option(symbol, oi, vol):
    return {"option": symbol, "open_interest": oi, "volume": vol, "iv": 0.2, "gamma": 0.001, "delta": 0.5}


def test_totals_span_all_live_expiries_and_strikes():
    today = datetime.date(2026, 10, 26)
    payload = {"data": {"current_price": 100.0, "options": [
        _option("SPY261026C00100000", 10, 5),    # today, near spot
        _option("SPY261218P00100000", 20, 7),    # later expiry
        _option("SPY261218P00300000", 30, 0),    # far outside the strike range
        _option("SPY261218C00100000", 0, 4),     # zero OI, still traded
        _option("SPY261023C00100000", 999, 999), # expired: left out
        {"option": "garbage", "open_interest": 5},
    ]}}
    _, rec = host._compact_matrix_options_payload("SPY", payload, today=today)
    assert rec["totals"] == {"call_oi": 10, "put_oi": 50, "call_vol": 9, "put_vol": 7}
    # The Strike Chart data keeps its own filters.
    assert len(rec["opts"]) == 2


def test_screen_has_his_columns_and_no_ticker_field():
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    section = page.split('id="view-options-summary"', 1)[1].split("</section>", 1)[0]
    assert "ticker-form" not in section
    assert "expirationPicker" not in section
    headers = re.findall(r"<th[ >][^>]*>?([^<]*)</th>", section)
    assert headers == ["Ticker", "Spot", "P/C OI", "P/C Vol", "Call OI", "Put OI", "Call Vol", "Put Vol"]
    assert "Coming soon" not in section


def test_screen_path_still_served():
    status, body = _get(_make_app(), "/matrix/options-summary")
    assert status == 200
    assert b'id="pcTable"' in body


def test_screen_is_only_the_table():
    """His page is one DataTable: no title, explanation text, tiles, charts or refresh."""
    page = (host.MATRIX_WEB_DIR / "matrix.html").read_text(encoding="utf-8")
    section = page.split('id="view-options-summary"', 1)[1].split("</section>", 1)[0]
    assert len(re.findall(r"<table", section)) == 1
    for extra in ("<h1", "<h2", "<h3", "<canvas", "<button", "<form", "<input", "pc-note"):
        assert extra not in section
    script = (host.MATRIX_WEB_DIR / "matrix.js").read_text(encoding="utf-8")
    assert "As of" not in script.split("// ---------- P/C Ratio", 1)[1].split("// ----------", 1)[0]
