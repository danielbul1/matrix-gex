"""Bridge captured-traffic specs (from mitmproxy2swagger) into the MCP pipeline.

mitmproxy2swagger turns browser traffic (a HAR) into an OpenAPI spec, but it
does not emit ``operationId`` values — which the Tripity pipeline requires to
allowlist operations safely. This module fills in deterministic operationIds so
a captured spec flows into ``build_mcp_server`` unchanged, closing the gap
between "record a website" and "get an MCP tool".
"""

from __future__ import annotations

import re
from typing import Any

_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}


def _slugify(method: str, path: str) -> str:
    parts = [method.lower()]
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        # turn a path param like {id} into "by_id"
        param = re.fullmatch(r"\{(.+?)\}", segment)
        parts.append(f"by_{param.group(1)}" if param else segment)
    slug = "_".join(parts)
    slug = re.sub(r"[^0-9a-zA-Z_]+", "_", slug).strip("_")
    return slug or method.lower()


def relax_response_schemas(spec: dict[str, Any]) -> None:
    """Drop response body schemas from a captured spec.

    mitmproxy2swagger infers response schemas from a single example, so they are
    too strict for real traffic (e.g. a field that is an object in the sample but
    null in another response). Removing them stops the MCP layer from rejecting
    valid responses on output validation, without affecting request handling.
    """
    for item in (spec.get("paths") or {}).values():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            for response in (operation.get("responses") or {}).values():
                if isinstance(response, dict):
                    response.pop("content", None)


def add_missing_operation_ids(spec: dict[str, Any]) -> list[str]:
    """Assign a deterministic operationId to every operation that lacks one.

    Mutates ``spec`` in place and returns the list of operationIds present,
    ready to pass as ``allowed_operation_ids`` to build_mcp_server.
    """
    seen: set[str] = set()
    ids: list[str] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId")
            if not (isinstance(op_id, str) and op_id):
                op_id = _slugify(method, path)
                candidate = op_id
                n = 2
                while candidate in seen:
                    candidate = f"{op_id}_{n}"
                    n += 1
                op_id = candidate
                operation["operationId"] = op_id
            seen.add(op_id)
            ids.append(op_id)
    return ids
