from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from tripity_experiment.connector_manifest import ConnectorManifest
from tripity_experiment.connector_runtime import PublishedConnector


@dataclass(frozen=True)
class DeliveryTool:
    name: str
    method: str
    path: str
    risk: str
    summary: str | None
    reason: str
    display_name: str | None = None
    usage_hint: str | None = None


@dataclass(frozen=True)
class DeliveryLogEntry:
    connector_slug: str
    tool_name: str
    status_code: int
    success: bool
    latency_ms: float
    timestamp: float


@dataclass(frozen=True)
class CompanyDeliveryPacket:
    company_name: str
    slug: str
    mcp_url: str
    client_auth: dict[str, str]
    enabled_tools: tuple[DeliveryTool, ...]
    disabled_tools: tuple[DeliveryTool, ...]
    manifest_json: str
    logs: tuple[DeliveryLogEntry, ...]
    instructions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_company_delivery_packet(
    *,
    manifest: ConnectorManifest,
    published: PublishedConnector,
) -> CompanyDeliveryPacket:
    enabled_tools = tuple(
        DeliveryTool(
            name=tool.name,
            method=tool.method,
            path=tool.path,
            risk=tool.risk,
            summary=tool.summary,
            reason=tool.reason,
            display_name=tool.display_name,
            usage_hint=tool.usage_hint,
        )
        for tool in manifest.tools
        if tool.enabled
    )
    disabled_tools = tuple(
        DeliveryTool(
            name=tool.name,
            method=tool.method,
            path=tool.path,
            risk=tool.risk,
            summary=tool.summary,
            reason=tool.reason,
            display_name=tool.display_name,
            usage_hint=tool.usage_hint,
        )
        for tool in manifest.tools
        if not tool.enabled
    )
    logs = tuple(DeliveryLogEntry(**entry.__dict__) for entry in published.logs)
    return CompanyDeliveryPacket(
        company_name=manifest.company.name,
        slug=manifest.company.slug,
        mcp_url=published.local_mcp_url,
        client_auth={
            "type": "bearer",
            "token": published.access_token,
            "header": "Authorization: Bearer <token>",
        },
        enabled_tools=enabled_tools,
        disabled_tools=disabled_tools,
        manifest_json=manifest.to_json(),
        logs=logs,
        instructions=(
            "Add the MCP URL to your AI client.",
            "Use the client bearer token as the MCP Authorization token.",
            "Read tools are enabled by default; write tools stay disabled unless explicitly approved.",
            "Logs are metadata-only: tool, status, latency, timestamp.",
        ),
    )
