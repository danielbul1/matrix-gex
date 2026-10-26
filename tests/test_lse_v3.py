"""Tests for the LSE Data 3.0 parsing: v3 candle rows, the chain overlay that
keeps Cboe's Greeks, and the pipeline's candle-time parser."""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "railway-service" / "src"))
sys.path.insert(0, str(ROOT))


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
import fetch_lse  # noqa: E402


def test_v3_candle_rows_parse_as_utc():
    # /v3/export CSV and /v3/preview both use a "time" column in UTC.
    rows = [
        {"time": "2026-10-23 20:00:00", "open": "7620", "high": "7625", "low": "7619", "close": "7623.67",
         "volume": "0", "trades": "2"},
        {"time": "2026-10-23 19:59:00", "open": "7618", "high": "7621", "low": "7617", "close": "7620",
         "volume": "12", "trades": "3"},
    ]
    candles = host._matrix_lse_candles_payload(rows)
    assert [c["time"] for c in candles] == [1792785540, 1792785600]  # sorted, 19:59 and 20:00 UTC
    assert candles[-1]["close"] == 7623.67
    assert candles[0]["volume"] == 12


def test_chain_overlay_keeps_cboe_greeks_and_takes_live_tape():
    base = {
        "asof": "2026-10-23T16:14:59",
        "opts": [{"exp": "2026-10-30", "t": "C", "k": 760.0, "oi": 1000, "iv": 0.15, "g": 0.01, "d": 0.5, "vol": 3}],
    }
    # A v3 chain row: Greeks are a session old, volume is "volume_today".
    rows = [{
        "ticker": "SPY261030C00760000", "contract_type": "call", "strike": 760.0, "expiry": "2026-10-30",
        "iv": 0.99, "gamma": 0.5, "delta": 0.9, "volume_today": 250, "last_price": 4.2,
        "last_trade_at": "2026-10-23 19:59:58.123456789",
    }]
    merged = host._merge_matrix_lse_chain(base, rows)
    option = merged["opts"][0]
    assert (option["iv"], option["g"], option["d"]) == (0.15, 0.01, 0.5)
    assert option["vol"] == 250
    assert option["last"] == 4.2
    assert merged["lse_matches"] == 1
    assert merged["asof"] == "2026-10-23T16:14:59"  # last trade time is not a snapshot time


def test_pipeline_candle_time_is_utc_epoch():
    assert fetch_lse.parse_candle_time("2026-10-23 20:00:00") == 1792785600
    assert fetch_lse.parse_candle_time("2026-10-23T20:00:00Z") == 1792785600
