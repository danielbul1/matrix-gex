from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from tripity_experiment.openapi_intake import OperationPreview, build_openapi_preview


@dataclass(frozen=True)
class ConnectorToolDraft:
    operation_id: str
    method: str
    path: str
    summary: str | None
    risk: str
    enabled: bool
    reason: str


@dataclass(frozen=True)
class CompanyConnectorDraft:
    company_name: str
    slug: str
    mcp_path: str
    mcp_url: str
    api_base_url: str
    title: str
    version: str
    exposed_tools: tuple[ConnectorToolDraft, ...]
    excluded_tools: tuple[ConnectorToolDraft, ...]
    issues: tuple[str, ...]

    def public_summary(self) -> dict[str, Any]:
        """Return a customer-facing summary with no credentials/secrets."""
        return {
            "company_name": self.company_name,
            "slug": self.slug,
            "mcp_url": self.mcp_url,
            "api_base_url": self.api_base_url,
            "api_title": self.title,
            "api_version": self.version,
            "exposed_tools": [tool.__dict__ for tool in self.exposed_tools],
            "excluded_tools": [tool.__dict__ for tool in self.excluded_tools],
            "issues": list(self.issues),
        }


def company_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug:
        raise ValueError("Company name must contain at least one letter or number")
    return slug[:64].strip("-") or "company"


def _tool_from_operation(
    operation: OperationPreview,
    *,
    enabled: bool,
    reason: str,
) -> ConnectorToolDraft:
    if not operation.operation_id:
        raise ValueError("Operation without operationId cannot become a connector tool draft")
    return ConnectorToolDraft(
        operation_id=operation.operation_id,
        method=operation.method,
        path=operation.path,
        summary=operation.summary,
        risk=operation.risk,
        enabled=enabled,
        reason=reason,
    )


def build_company_connector_draft(
    *,
    company_name: str,
    openapi_spec: dict[str, Any],
    api_base_url: str,
    public_mcp_base_url: str,
    allow_write_operation_ids: set[str] | frozenset[str] = frozenset(),
) -> CompanyConnectorDraft:
    """Build the first customer-facing artifact for an approved company API.

    Read tools are exposed by default. Write/delete tools are excluded unless
    their operationId is explicitly present in ``allow_write_operation_ids``.
    """
    preview = build_openapi_preview(openapi_spec)
    slug = company_slug(company_name)
    base = public_mcp_base_url.rstrip("/")
    mcp_path = f"/mcp/{slug}"
    allowed_writes = frozenset(allow_write_operation_ids)
    exposed: list[ConnectorToolDraft] = []
    excluded: list[ConnectorToolDraft] = []
    issues = [issue.message for issue in preview.issues if issue.severity == "error"]

    for operation in preview.operations:
        if not operation.operation_id:
            continue
        if operation.risk == "read":
            exposed.append(
                _tool_from_operation(
                    operation,
                    enabled=True,
                    reason="read-only default",
                )
            )
        elif operation.operation_id in allowed_writes:
            exposed.append(
                _tool_from_operation(
                    operation,
                    enabled=True,
                    reason="explicit write opt-in",
                )
            )
        else:
            excluded.append(
                _tool_from_operation(
                    operation,
                    enabled=False,
                    reason="write/delete excluded by default",
                )
            )

    if not exposed:
        issues.append("No read-only operations are available to expose by default")

    return CompanyConnectorDraft(
        company_name=company_name,
        slug=slug,
        mcp_path=mcp_path,
        mcp_url=f"{base}{mcp_path}",
        api_base_url=api_base_url.rstrip("/"),
        title=preview.title,
        version=preview.version,
        exposed_tools=tuple(exposed),
        excluded_tools=tuple(excluded),
        issues=tuple(issues),
    )
