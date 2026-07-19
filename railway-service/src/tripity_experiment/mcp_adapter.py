from __future__ import annotations

from copy import deepcopy
from collections.abc import Collection
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType
from fastmcp.utilities.openapi import HTTPRoute

from tripity_experiment.openapi_intake import company_connector_operation_ids


def hide_server_managed_headers(
    openapi_spec: dict[str, Any],
    header_names: Collection[str],
) -> dict[str, Any]:
    """Remove secret headers from operation inputs before tools are generated."""
    sanitized = deepcopy(openapi_spec)
    hidden = {name.casefold() for name in header_names}

    for path_item in sanitized.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        path_item["parameters"] = [
            parameter
            for parameter in path_item.get("parameters", [])
            if not (
                isinstance(parameter, dict)
                and parameter.get("in") == "header"
                and str(parameter.get("name", "")).casefold() in hidden
            )
        ]
        for method, operation in path_item.items():
            if method.lower() not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "options",
                "head",
                "trace",
            } or not isinstance(operation, dict):
                continue
            operation["parameters"] = [
                parameter
                for parameter in operation.get("parameters", [])
                if not (
                    isinstance(parameter, dict)
                    and parameter.get("in") == "header"
                    and str(parameter.get("name", "")).casefold() in hidden
                )
            ]

    return sanitized


def build_mcp_server(
    *,
    openapi_spec: dict[str, Any],
    base_url: str,
    allowed_operation_ids: Collection[str] | None = None,
    allow_write_operation_ids: Collection[str] = (),
    bearer_token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    auth: Any = None,
) -> FastMCP:
    """Build an MCP server without endpoint-specific handlers.

    ``auth`` is an optional FastMCP auth provider (e.g. an OAuth provider). When
    supplied, the server advertises OAuth discovery/authorize/token/register
    routes and protects the MCP endpoint. It is the alternative to the static
    client Bearer gate applied by the sidecar.
    """
    allowed = frozenset(
        allowed_operation_ids
        if allowed_operation_ids is not None
        else company_connector_operation_ids(
            openapi_spec,
            allow_write_operation_ids=allow_write_operation_ids,
        )
    )
    # A default User-Agent: some real APIs (e.g. Wikimedia) reject blank-UA
    # requests with 403. A caller's bearer token still takes precedence.
    headers: dict[str, str] = {"User-Agent": "Tripity/1.0 (+https://trytripity.site)"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    upstream = httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        transport=transport,
        timeout=httpx.Timeout(5),
        follow_redirects=False,
    )

    def allowlist(route: HTTPRoute, _default: MCPType) -> MCPType:
        if route.operation_id in allowed:
            return MCPType.TOOL
        return MCPType.EXCLUDE

    server = FastMCP.from_openapi(
        openapi_spec=hide_server_managed_headers(
            openapi_spec,
            header_names={"authorization"},
        ),
        client=upstream,
        route_map_fn=allowlist,
        name="Tripity OpenAPI Experiment",
    )
    if auth is not None:
        server.auth = auth
    return server
