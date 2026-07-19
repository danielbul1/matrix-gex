from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit


def _schema_for_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, list):
        item_schema = _schema_for_value(value[0]) if value else {}
        return {"type": "array", "items": item_schema}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {str(k): _schema_for_value(v) for k, v in value.items()},
        }
    return {"type": "string"}


def _json_body_schema(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    import json

    try:
        return _schema_for_value(json.loads(text))
    except Exception:  # noqa: BLE001 - HAR payloads are often not JSON
        return None


def _operation_id(method: str, path: str) -> str:
    clean = path.strip("/") or "root"
    parts = [p for p in clean.replace("-", "_").replace(".", "_").split("/") if p]
    normalized = "_".join("by" if p.startswith("{") else p.strip("{}") for p in parts)
    return f"{method.lower()}_{normalized}"[:80]


def _path_template(path: str) -> str:
    # Keep the minimum safe behavior: do not infer path parameters yet. Inference
    # needs human review, so exact paths are safer for the first draft.
    return path or "/"


def har_to_openapi(har: dict[str, Any], *, title: str = "Tripity HAR Draft API") -> dict[str, Any]:
    """Convert a browser HAR object into a conservative OpenAPI 3.1 draft.

    Secrets are intentionally not copied: cookies, headers, request payload values,
    and response examples are omitted. Only method/path/query names and coarse JSON
    schemas are used.
    """

    entries = (((har.get("log") or {}).get("entries")) or []) if isinstance(har, dict) else []
    if not isinstance(entries, list) or not entries:
        raise ValueError("HAR must contain log.entries")

    origins: list[str] = []
    paths: dict[str, Any] = {}
    seen_ops: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        if not isinstance(request, dict):
            continue
        method = str(request.get("method") or "GET").lower()
        if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
            continue
        raw_url = str(request.get("url") or "")
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in origins:
            origins.append(origin)
        path = _path_template(parsed.path or "/")
        item = paths.setdefault(path, {})
        if method in item:
            continue

        query_names = {name for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        for param in request.get("queryString") or []:
            if isinstance(param, dict) and param.get("name"):
                query_names.add(str(param["name"]))

        op_id = _operation_id(method, path)
        suffix = 2
        base_op_id = op_id
        while op_id in seen_ops:
            op_id = f"{base_op_id}_{suffix}"
            suffix += 1
        seen_ops.add(op_id)

        operation: dict[str, Any] = {
            "operationId": op_id,
            "summary": f"{method.upper()} {path}",
            "description": "Drafted from approved HAR traffic. Review before production use.",
            "parameters": [
                {
                    "name": name,
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                }
                for name in sorted(query_names)
            ],
            "responses": {
                "200": {
                    "description": "Successful response",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }
            },
        }

        post_data = request.get("postData") or {}
        body_schema = _json_body_schema(post_data.get("text") if isinstance(post_data, dict) else None)
        if body_schema is not None and method in {"post", "put", "patch"}:
            operation["requestBody"] = {
                "required": False,
                "content": {"application/json": {"schema": body_schema}},
            }

        item[method] = operation

    if not paths:
        raise ValueError("HAR did not contain usable HTTP API requests")

    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": "0.1.0",
            "description": "Draft OpenAPI generated from approved HAR traffic by Tripity.",
        },
        "servers": [{"url": origins[0]}],
        "paths": paths,
    }
