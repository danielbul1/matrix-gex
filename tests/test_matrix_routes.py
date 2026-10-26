"""Matrix keeps only the screens Quant Power has (ADR 0001).

The page still loads, and the endpoints that served deleted screens (flow,
replay, live spot polling, regime, journal) answer 404. Starting the app
removes the old SQLite store those screens wrote to.
"""
import asyncio
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


def _get(app, path):
    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": []}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


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
