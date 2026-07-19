"""Tripity meta-gateway — one MCP server that connects APIs on demand.

Instead of one sidecar per API added manually, the AI connects to this single
gateway once. From inside the conversation, calling ``connect_api`` fetches an
OpenAPI spec, exposes the chosen operations as new tools at runtime, and emits
a ``tools/list_changed`` notification so the client picks them up live — the
core of "create the MCP and connect it automatically, from inside the AI".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit
from typing import Any

import httpx

from fastmcp import Client, Context, FastMCP
from fastmcp.client.auth import BearerAuth
from fastmcp.server import create_proxy

from tripity_experiment.browser_connector import (
    CdpSessionExecutor,
    SessionExecutor,
    register_origin_query,
)
from tripity_experiment.catalog import CURATED, CatalogEntry, find_entry
from tripity_experiment.connection_store import Connection, ConnectionStore
from tripity_experiment.garmin_recipe import is_garmin_origin, register_garmin_tools
from tripity_experiment.mcp_adapter import build_mcp_server
from tripity_experiment.openapi_intake import fetch_openapi_url

SpecFetcher = Callable[[str], Awaitable[dict[str, Any]]]
# Builds the SessionExecutor for a browser connection's origin. Injectable so
# tests can supply a fake instead of attaching to a real Chrome.
BrowserExecutorFactory = Callable[[str], SessionExecutor]


def _default_browser_executor(origin: str) -> SessionExecutor:
    return CdpSessionExecutor(origin=origin)


async def _mount_connection(
    gateway: FastMCP,
    connection: Connection,
    spec_fetcher: SpecFetcher,
    upstream_transport: httpx.AsyncBaseTransport | None,
    browser_executor_factory: BrowserExecutorFactory = _default_browser_executor,
) -> None:
    if connection.kind == "browser":
        if not connection.origin:
            raise ValueError("Browser connection is missing its origin")
        # One generic tool named after the connection gives the AI full access to
        # that site, run inside the user's authenticated Chrome session.
        subserver: FastMCP = FastMCP(name=connection.name)
        executor = browser_executor_factory(connection.origin)
        # Discover the site's endpoints from the user's browsing the first time,
        # so the AI gets a map (persisted on the connection for later sessions).
        if not connection.endpoints and hasattr(executor, "discover"):
            try:
                connection.endpoints = await executor.discover()
            except Exception:  # noqa: BLE001 - discovery is a best-effort nicety
                connection.endpoints = []
        register_origin_query(
            subserver,
            connection.origin,
            executor,
            tool_name=connection.name,
            endpoints=connection.endpoints,
        )
        if is_garmin_origin(connection.origin):
            register_garmin_tools(
                subserver, executor, prefix=connection.name, endpoints=connection.endpoints
            )
        gateway.mount(subserver)
        return
    if connection.kind == "mcp":
        if not connection.mcp_url:
            raise ValueError("MCP connection is missing its URL")
        auth = BearerAuth(connection.api_bearer_token) if connection.api_bearer_token else None
        gateway.mount(create_proxy(Client(connection.mcp_url, auth=auth)))
        return
    spec = await spec_fetcher(connection.openapi_url)
    subserver = build_mcp_server(
        openapi_spec=spec,
        base_url=connection.base_url,
        allowed_operation_ids=connection.operations,
        bearer_token=connection.api_bearer_token,
        transport=upstream_transport,
    )
    gateway.mount(subserver)


async def restore_connections(
    gateway: FastMCP,
    store: ConnectionStore,
    *,
    spec_fetcher: SpecFetcher = fetch_openapi_url,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    owner_id: str | None = None,
    browser_executor_factory: BrowserExecutorFactory = _default_browser_executor,
) -> list[str]:
    """Re-mount every saved connection so its tools exist at startup."""
    restored: list[str] = []
    for connection in store.load(owner_id):
        try:
            await _mount_connection(
                gateway,
                connection,
                spec_fetcher,
                upstream_transport,
                browser_executor_factory,
            )
            restored.append(connection.name)
        except Exception:  # noqa: BLE001 - a stale/unreachable spec must not block startup
            continue
    return restored


def build_gateway(
    *,
    name: str = "Tripity Gateway",
    store: ConnectionStore | None = None,
    catalog: tuple[CatalogEntry, ...] = CURATED,
    spec_fetcher: SpecFetcher = fetch_openapi_url,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    owner_id: str = "legacy",
    browser_executor_factory: BrowserExecutorFactory = _default_browser_executor,
) -> FastMCP:
    """Build the gateway server exposing the meta-tools.

    Tools: ``list_catalog`` (browse known APIs), ``connect_from_catalog``
    (connect one by name — no links), ``connect_api`` (connect any API by URL),
    and ``connect_site`` (connect a website the user is logged into, giving the
    AI full access to it through the user's browser session). When ``store`` is
    given, connections persist and restore on startup. ``spec_fetcher``,
    ``upstream_transport`` and ``browser_executor_factory`` are injectable for
    testing.
    """
    gateway: FastMCP = FastMCP(name=name)

    async def _connect_and_register(connection: Connection, ctx: Context) -> None:
        connection.owner_id = owner_id
        await _mount_connection(
            gateway,
            connection,
            spec_fetcher,
            upstream_transport,
            browser_executor_factory,
        )
        if store is not None:
            store.add(connection)
        # Tell the connected AI its toolset changed so it re-lists immediately.
        await ctx.session.send_tool_list_changed()

    @gateway.tool
    async def connect_site(origin: str, name: str, ctx: Context) -> str:
        """Connect a website you are logged into (e.g. your CRM).

        ``origin`` is the site root (e.g. https://crm.example.com); ``name`` is
        what the new tool is called. Afterwards the AI can call ``name`` with any
        path/params to reach the whole site, run inside your browser session.
        """
        origin = origin.rstrip("/")
        await _connect_and_register(
            Connection(name=name, kind="browser", origin=origin), ctx
        )
        return (
            f"Connected site '{name}' ({origin}). Call the '{name}' tool with a "
            "path to read anything you can see there. Saved for future sessions."
        )

    @gateway.tool
    async def list_catalog() -> list[dict[str, Any]]:
        """List the APIs available to connect by name."""
        return [
            {"name": e.name, "description": e.description, "operations": list(e.operations)}
            for e in catalog
        ]

    @gateway.tool
    async def connect_from_catalog(
        name: str,
        ctx: Context,
        api_bearer_token: str | None = None,
    ) -> str:
        """Connect a catalog API by name (no links needed), e.g. 'petstore'."""
        entry = find_entry(name, catalog)
        if entry is None:
            available = ", ".join(e.name for e in catalog)
            raise ValueError(f"Unknown API '{name}'. Available: {available}")
        await _connect_and_register(
            Connection(
                name=entry.name,
                openapi_url=entry.openapi_url,
                base_url=entry.base_url,
                operations=list(entry.operations),
                api_bearer_token=api_bearer_token,
            ),
            ctx,
        )
        return f"Connected '{entry.name}': {', '.join(entry.operations)} (saved for future sessions)."

    @gateway.tool
    async def connect_api(
        openapi_url: str,
        base_url: str,
        operations: list[str],
        ctx: Context,
        api_bearer_token: str | None = None,
        name: str | None = None,
    ) -> str:
        """Connect any external API by URL and expose its chosen operations.

        openapi_url: URL of the API's OpenAPI document.
        base_url: the API's base URL for making calls.
        operations: operationIds to expose (from the API's spec).
        api_bearer_token: optional bearer token for the API's own auth.
        name: a short name for this connection (defaults to the API's host).
        """
        if not operations:
            raise ValueError("Provide at least one operation to expose")
        await _connect_and_register(
            Connection(
                name=name or (urlsplit(base_url).hostname or "api"),
                openapi_url=openapi_url,
                base_url=base_url,
                operations=operations,
                api_bearer_token=api_bearer_token,
            ),
            ctx,
        )
        saved = " and saved for future sessions" if store is not None else ""
        return f"Connected {len(operations)} operation(s): {', '.join(operations)}{saved}"

    @gateway.tool
    async def connect_mcp(
        mcp_url: str,
        ctx: Context,
        name: str,
        mcp_bearer_token: str | None = None,
    ) -> str:
        """Connect an existing Streamable HTTP MCP server to this personal gateway."""
        if not mcp_url.startswith(("http://", "https://")):
            raise ValueError("MCP URL must use http or https")
        await _connect_and_register(
            Connection(
                name=name,
                openapi_url="",
                base_url="",
                api_bearer_token=mcp_bearer_token,
                kind="mcp",
                mcp_url=mcp_url,
            ),
            ctx,
        )
        return f"Connected MCP '{name}'{(' and saved' if store is not None else '')}"

    return gateway


def main() -> None:
    import asyncio
    import os

    import uvicorn

    store = ConnectionStore(os.getenv("TRIPITY_STORE", "tripity_connections.json"))
    gateway = build_gateway(store=store)
    # Reload saved connections so their tools exist before the server serves.
    restored = asyncio.run(restore_connections(gateway, store))
    if restored:
        print(f"Restored connections: {', '.join(restored)}")
    app = gateway.http_app(path="/mcp", stateless_http=False)
    uvicorn.run(
        app,
        host=os.getenv("TRIPITY_HOST", "127.0.0.1"),
        port=int(os.getenv("TRIPITY_PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
