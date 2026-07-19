from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Literal

from tripity_experiment.company_connector import CompanyConnectorDraft

SCHEMA_VERSION = "tripity.connector.v0"


@dataclass(frozen=True)
class ManifestCompany:
    name: str
    slug: str


@dataclass(frozen=True)
class ManifestSource:
    type: Literal["openapi"]
    api_base_url: str
    api_title: str
    api_version: str


@dataclass(frozen=True)
class ManifestRuntime:
    mcp_path: str
    mcp_url: str


@dataclass(frozen=True)
class ManifestPolicy:
    default_access: Literal["read_only"] = "read_only"
    writes_require_explicit_opt_in: bool = True
    destructive_actions_require_human_confirmation: bool = True


@dataclass(frozen=True)
class ManifestTool:
    name: str
    method: str
    path: str
    risk: str
    enabled: bool
    reason: str
    summary: str | None = None
    display_name: str | None = None
    usage_hint: str | None = None


@dataclass(frozen=True)
class ManifestLogging:
    metadata_logging: Literal["on"] = "on"
    payload_logging: Literal["off"] = "off"


@dataclass(frozen=True)
class ConnectorManifest:
    schema_version: str
    company: ManifestCompany
    source: ManifestSource
    runtime: ManifestRuntime
    policy: ManifestPolicy
    tools: tuple[ManifestTool, ...]
    logging: ManifestLogging
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConnectorManifest":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported connector manifest schema_version")
        return cls(
            schema_version=str(data["schema_version"]),
            company=ManifestCompany(**data["company"]),
            source=ManifestSource(**data["source"]),
            runtime=ManifestRuntime(**data["runtime"]),
            policy=ManifestPolicy(**data.get("policy", {})),
            tools=tuple(ManifestTool(**tool) for tool in data.get("tools", [])),
            logging=ManifestLogging(**data.get("logging", {})),
            issues=tuple(str(issue) for issue in data.get("issues", ())),
        )

    @classmethod
    def from_json(cls, text: str) -> "ConnectorManifest":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("Connector manifest JSON must be an object")
        return cls.from_dict(raw)

    def enabled_tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools if tool.enabled)

    def disabled_tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools if not tool.enabled)


def manifest_from_draft(draft: CompanyConnectorDraft) -> ConnectorManifest:
    from tripity_experiment.semantic_curation import curate_manifest_tools

    raw_tools = tuple(
        ManifestTool(
            name=tool.operation_id,
            method=tool.method,
            path=tool.path,
            risk=tool.risk,
            enabled=tool.enabled,
            reason=tool.reason,
            summary=tool.summary,
        )
        for tool in (*draft.exposed_tools, *draft.excluded_tools)
    )
    tools = list(curate_manifest_tools(raw_tools))
    return ConnectorManifest(
        schema_version=SCHEMA_VERSION,
        company=ManifestCompany(name=draft.company_name, slug=draft.slug),
        source=ManifestSource(
            type="openapi",
            api_base_url=draft.api_base_url,
            api_title=draft.title,
            api_version=draft.version,
        ),
        runtime=ManifestRuntime(mcp_path=draft.mcp_path, mcp_url=draft.mcp_url),
        policy=ManifestPolicy(),
        tools=tuple(tools),
        logging=ManifestLogging(),
        issues=draft.issues,
    )
