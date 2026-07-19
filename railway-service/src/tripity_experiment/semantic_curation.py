"""Semantic tool-quality layer for company connectors.

Sits above the raw OpenAPI: derives a friendly display name and a "use this
when..." hint for each read tool, and hides non-data authentication/session
operations by default. Driven by heuristics; every result can be overridden by
editing the connector manifest. No LLM, no change to the underlying operationId
(routing/logging match on it).
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from tripity_experiment.connector_manifest import ManifestTool

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")

# Unambiguous auth/session *actions* — hidden because they are not company data.
# Kept conservative on purpose so real data tools (e.g. getUserByName) are not hit.
_AUTH_OP_RE = re.compile(r"(?i)(?:^|[_\-])(log[_\-]?in|log[_\-]?out|sign[_\-]?in|sign[_\-]?out|authenticate|refresh[_\-]?token)(?:$|[_\-]|user\b|session\b)")


def is_non_data_auth_op(operation_id: str) -> bool:
    """True for login/logout/signin/signout/authenticate style operations."""
    name = operation_id or ""
    if _AUTH_OP_RE.search(name):
        return True
    lowered = name.lower()
    return lowered in {"login", "logout", "signin", "signout", "authenticate"}


def derive_display_name(operation_id: str) -> str:
    """`getPetById` -> `Get Pet By Id`; `find_pets` -> `Find Pets`."""
    if not operation_id:
        return operation_id
    spaced = re.sub(r"[_\-]+", " ", operation_id)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    words = [w for w in spaced.split() if w]
    if not words:
        return operation_id
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _singular(word: str) -> str:
    word = word.strip("/").lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def derive_usage_hint(name: str, method: str, path: str) -> str:
    """A short, honest 'use this when...' guidance derived from the operation."""
    parts = path.split("/")
    non_param = [seg for seg in parts if seg and not seg.startswith("{")]
    first = non_param[0] if non_param else "record"

    id_resource: str | None = None
    for index, seg in enumerate(parts):
        if seg.startswith("{") and index > 0:
            previous = parts[index - 1]
            if previous and not previous.startswith("{"):
                id_resource = previous
                break
    has_id = id_resource is not None or "{" in path

    lname = (name or "").lower()
    is_search = any(key in lname for key in ("find", "search", "list", "query", "all"))

    upper = (method or "").upper()
    if upper in ("GET", "HEAD"):
        if is_search:
            return f"Use this to search or list {first} records."
        if has_id:
            return f"Use this to fetch a single {_singular(id_resource or first)} by its identifier."
        return f"Use this to read {_singular(non_param[-1] if non_param else first)} data."
    return f"Use this to perform a {upper} action on {first} (write — off by default)."


def curate_manifest_tools(tools: tuple[ManifestTool, ...]) -> tuple[ManifestTool, ...]:
    """Fill display_name/usage_hint and hide non-data auth operations."""
    curated: list[ManifestTool] = []
    for tool in tools:
        display_name = tool.display_name or derive_display_name(tool.name)
        usage_hint = tool.usage_hint or derive_usage_hint(tool.name, tool.method, tool.path)
        enabled = tool.enabled
        reason = tool.reason
        if enabled and is_non_data_auth_op(tool.name):
            enabled = False
            reason = "hidden by default: authentication/session action, not company data"
        curated.append(
            ManifestTool(
                name=tool.name,
                method=tool.method,
                path=tool.path,
                risk=tool.risk,
                enabled=enabled,
                reason=reason,
                summary=tool.summary,
                display_name=display_name,
                usage_hint=usage_hint,
            )
        )
    curated.sort(key=lambda tool: (not tool.enabled, tool.name))
    return tuple(curated)


def relax_write_response_schemas(
    openapi_spec: dict[str, Any],
    tools: tuple[ManifestTool, ...],
) -> dict[str, Any]:
    """Loosen response schemas for enabled write operations.

    A write is intercepted by the human-approval gate and answered with a synthetic
    ``pending_human_approval`` body, which won't match the operation's declared
    success schema. Since the real write response never reaches the AI before
    approval, we relax these response schemas so the pending message passes tool
    output validation instead of raising. Read operations are left strict.
    """
    write_ops = {
        tool.name
        for tool in tools
        if tool.enabled and (tool.method or "").upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }
    if not write_ops:
        return openapi_spec
    spec = deepcopy(openapi_spec)
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            if operation.get("operationId") not in write_ops:
                continue
            responses = operation.get("responses", {})
            if isinstance(responses, dict):
                for response in responses.values():
                    if isinstance(response, dict):
                        for media in response.get("content", {}).values():
                            if isinstance(media, dict):
                                media["schema"] = {"type": "object"}
    return spec


def apply_tool_curation_to_spec(
    openapi_spec: dict[str, Any],
    tools: tuple[ManifestTool, ...],
) -> dict[str, Any]:
    """Rewrite enabled tools' operation description so the AI sees the usage hint.

    The underlying operationId is untouched; only summary/description text changes.
    """
    by_operation_id = {tool.name: tool for tool in tools if tool.enabled}
    if not by_operation_id:
        return openapi_spec
    spec = deepcopy(openapi_spec)
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            tool = by_operation_id.get(operation.get("operationId"))
            if tool is None:
                continue
            original = (operation.get("description") or operation.get("summary") or "").strip()
            hint = tool.usage_hint or ""
            if original and hint and original.lower() not in hint.lower():
                operation["description"] = f"{hint}\n\n{original}"
            elif hint:
                operation["description"] = hint
            if tool.display_name:
                operation["summary"] = tool.display_name
    return spec
