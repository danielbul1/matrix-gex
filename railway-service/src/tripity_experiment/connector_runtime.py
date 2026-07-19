from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

from tripity_experiment.connector_manifest import ConnectorManifest, ManifestTool
from tripity_experiment.mcp_adapter import build_mcp_server
from tripity_experiment.project_gateway import BearerGate
from tripity_experiment.semantic_curation import (
    apply_tool_curation_to_spec,
    relax_write_response_schemas,
)


@dataclass(frozen=True)
class ToolCallLogEntry:
    connector_slug: str
    tool_name: str
    status_code: int
    success: bool
    latency_ms: float
    timestamp: float


class MetadataLoggingTransport(httpx.AsyncBaseTransport):
    """httpx transport wrapper that records upstream tool-call metadata only."""

    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        *,
        connector_slug: str,
        tools: tuple[ManifestTool, ...],
        logs: list[ToolCallLogEntry],
        base_path: str = "",
    ) -> None:
        self.wrapped = wrapped
        self.connector_slug = connector_slug
        self.logs = logs
        self.base_path = base_path.rstrip("/")
        self.routes = [
            (tool.name, tool.method.upper(), _path_template_regex(tool.path))
            for tool in tools
            if tool.enabled
        ]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        tool_name = self._match_tool(request)
        started = time.monotonic()
        response = await self.wrapped.handle_async_request(request)
        if tool_name is not None:
            self.logs.append(
                ToolCallLogEntry(
                    connector_slug=self.connector_slug,
                    tool_name=tool_name,
                    status_code=response.status_code,
                    success=200 <= response.status_code < 400,
                    latency_ms=round((time.monotonic() - started) * 1000, 3),
                    timestamp=time.time(),
                )
            )
        return response

    def _match_tool(self, request: httpx.Request) -> str | None:
        path = request.url.path
        if self.base_path and path.startswith(self.base_path + "/"):
            path = path[len(self.base_path):]
        method = request.method.upper()
        for tool_name, route_method, pattern in self.routes:
            if method == route_method and pattern.fullmatch(path):
                return tool_name
        return None

    async def aclose(self) -> None:
        await self.wrapped.aclose()


def _path_template_regex(path: str) -> re.Pattern[str]:
    escaped = re.escape(path)
    pattern = re.sub(r"\\\{[^/]+\\\}", r"[^/]+", escaped)
    return re.compile(pattern)


@dataclass(frozen=True)
class PendingApproval:
    """Public-safe record of a write the AI proposed, awaiting human approval.

    Deliberately carries no payload/argument values — the request needed to replay
    the write is held separately in the gate's private map, never serialized.
    """

    approval_id: str
    connector_slug: str
    tool_name: str
    method: str
    path: str
    created_at: float


class WriteApprovalGate(httpx.AsyncBaseTransport):
    """Holds AI-proposed writes for out-of-band human approval.

    Write requests (POST/PUT/PATCH/DELETE) are not forwarded upstream; they are
    recorded as a pending approval and answered with a synthetic ``202`` telling the
    AI the action needs human approval. Reads pass straight through. A human approves
    later via :meth:`approve`, which replays the exact request upstream.
    """

    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        *,
        connector_slug: str,
        tools: tuple[ManifestTool, ...],
        logs: list[ToolCallLogEntry],
        pending: list[PendingApproval],
        base_path: str = "",
    ) -> None:
        self.wrapped = wrapped
        self.connector_slug = connector_slug
        self.logs = logs
        self.pending = pending
        self.base_path = base_path.rstrip("/")
        self.routes = [
            (tool.name, tool.method.upper(), _path_template_regex(tool.path))
            for tool in tools
            if tool.enabled
        ]
        # approval_id -> (method, url, headers, content) — private replay data.
        self._replay: dict[str, tuple[str, httpx.URL, httpx.Headers, bytes]] = {}

    def _match_tool(self, request: httpx.Request) -> str | None:
        path = request.url.path
        if self.base_path and path.startswith(self.base_path + "/"):
            path = path[len(self.base_path):]
        method = request.method.upper()
        for tool_name, route_method, pattern in self.routes:
            if method == route_method and pattern.fullmatch(path):
                return tool_name
        return None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() not in WRITE_METHODS:
            return await self.wrapped.handle_async_request(request)
        tool_name = self._match_tool(request) or f"({request.method.lower()})"
        approval_id = secrets.token_urlsafe(9)
        self._replay[approval_id] = (
            request.method,
            request.url,
            request.headers.copy(),
            request.content,
        )
        self.pending.append(
            PendingApproval(
                approval_id=approval_id,
                connector_slug=self.connector_slug,
                tool_name=tool_name,
                method=request.method.upper(),
                path=request.url.path,
                created_at=time.time(),
            )
        )
        body = json.dumps(
            {
                "status": "pending_human_approval",
                "approval_id": approval_id,
                "message": (
                    "This write was recorded and needs human approval in the Tripity "
                    "portal before it runs. Nothing has changed yet."
                ),
            }
        ).encode("utf-8")
        return httpx.Response(
            202,
            headers={"content-type": "application/json"},
            content=body,
            request=request,
        )

    async def approve(self, approval_id: str) -> httpx.Response:
        if approval_id not in self._replay:
            raise KeyError(approval_id)
        method, url, headers, content = self._replay.pop(approval_id)
        entry = next((p for p in self.pending if p.approval_id == approval_id), None)
        if entry is not None:
            self.pending.remove(entry)
        replay = httpx.Request(method=method, url=url, headers=headers, content=content)
        started = time.monotonic()
        response = await self.wrapped.handle_async_request(replay)
        await response.aread()
        self.logs.append(
            ToolCallLogEntry(
                connector_slug=self.connector_slug,
                tool_name=entry.tool_name if entry else f"({method.lower()})",
                status_code=response.status_code,
                success=200 <= response.status_code < 400,
                latency_ms=round((time.monotonic() - started) * 1000, 3),
                timestamp=time.time(),
            )
        )
        return response


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class PublishedConnector:
    slug: str
    access_token: str
    local_mcp_url: str
    manifest_path: str
    operation_ids: tuple[str, ...]
    logs: list[ToolCallLogEntry]
    server: uvicorn.Server
    thread: threading.Thread
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    approval_gate: WriteApprovalGate | None = None

    async def approve(self, approval_id: str) -> int:
        """Approve and execute a pending write; returns the upstream status code."""
        if self.approval_gate is None:
            raise KeyError(approval_id)
        response = await self.approval_gate.approve(approval_id)
        return response.status_code

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class ManifestMcpPublisher:
    """Local/in-memory publisher for Tripity connector manifests.

    This is the first runtime bridge from Tripity's internal connector recipe to
    an actual MCP endpoint. It is intentionally local and ephemeral; production
    persistence/routing is a later loop.
    """

    def __init__(self) -> None:
        self.connectors: dict[str, PublishedConnector] = {}

    def publish(
        self,
        *,
        manifest: ConnectorManifest,
        openapi_spec: dict[str, Any],
        upstream_bearer_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client_token: str | None = None,
    ) -> PublishedConnector:
        enabled_tools = tuple(tool for tool in manifest.tools if tool.enabled)
        enabled_operation_ids = [tool.name for tool in enabled_tools]
        if not enabled_operation_ids:
            raise ValueError("Connector manifest has no enabled tools to publish")

        client_token = client_token or secrets.token_urlsafe(32)
        port = _free_port()
        path = manifest.runtime.mcp_path
        logs: list[ToolCallLogEntry] = []
        pending: list[PendingApproval] = []
        base_path = urlsplit(manifest.source.api_base_url).path
        approval_gate = WriteApprovalGate(
            transport or httpx.AsyncHTTPTransport(),
            connector_slug=manifest.company.slug,
            tools=enabled_tools,
            logs=logs,
            pending=pending,
            base_path=base_path,
        )
        upstream_transport = MetadataLoggingTransport(
            approval_gate,
            connector_slug=manifest.company.slug,
            tools=enabled_tools,
            logs=logs,
            base_path=base_path,
        )
        curated_spec = apply_tool_curation_to_spec(openapi_spec, manifest.tools)
        curated_spec = relax_write_response_schemas(curated_spec, manifest.tools)
        mcp = build_mcp_server(
            openapi_spec=curated_spec,
            base_url=manifest.source.api_base_url,
            allowed_operation_ids=enabled_operation_ids,
            bearer_token=upstream_bearer_token,
            transport=upstream_transport,
        )
        protected_app = BearerGate(
            mcp.http_app(path=path, stateless_http=True),
            client_token,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                protected_app,
                host="127.0.0.1",
                port=port,
                log_level="error",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=2)
            raise RuntimeError("Manifest MCP server did not start")

        published = PublishedConnector(
            slug=manifest.company.slug,
            access_token=client_token,
            local_mcp_url=f"http://127.0.0.1:{port}{path}",
            manifest_path=path,
            operation_ids=tuple(enabled_operation_ids),
            logs=logs,
            server=server,
            thread=thread,
            pending_approvals=pending,
            approval_gate=approval_gate,
        )
        self.connectors[manifest.company.slug] = published
        return published

    def close(self) -> None:
        for connector in list(self.connectors.values()):
            connector.stop()
        self.connectors.clear()
