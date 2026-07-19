from __future__ import annotations

from tripity_experiment.company_delivery import CompanyDeliveryPacket


def build_company_export_text(packet: CompanyDeliveryPacket) -> str:
    """Build a shareable company-facing summary without secrets.

    The export intentionally excludes the MCP client token, upstream credentials,
    request arguments, and response payloads. It is safe to send after a demo.
    """
    enabled = "\n".join(
        f"- {tool.name} ({tool.method} {tool.path}) — {tool.reason}"
        for tool in packet.enabled_tools
    ) or "- none"
    disabled = "\n".join(
        f"- {tool.name} ({tool.method} {tool.path}) — {tool.reason}"
        for tool in packet.disabled_tools
    ) or "- none"
    logs = "\n".join(
        f"- {log.tool_name}: {log.status_code}, success={str(log.success).lower()}, latency={log.latency_ms}ms"
        for log in packet.logs
    ) or "- no AI calls logged yet"

    return "\n".join(
        [
            "Tripity AI Connector Demo Packet",
            "=================================",
            "",
            f"Company: {packet.company_name}",
            f"Slug: {packet.slug}",
            f"MCP URL: {packet.mcp_url}",
            "Auth: Bearer token required (token omitted from export)",
            "Manifest: tripity.connector.v0",
            "Policy: read-only by default; writes require explicit approval",
            "Logging: metadata-only; payload logging off",
            "",
            "Enabled read tools:",
            enabled,
            "",
            "Disabled write tools:",
            disabled,
            "",
            "Metadata-only activity:",
            logs,
            "",
            "Sensitive data omitted: client token, upstream token, request arguments, response payloads.",
        ]
    )
