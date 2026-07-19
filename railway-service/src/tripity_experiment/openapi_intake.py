from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
import ipaddress
import json
import socket
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import yaml

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")
READ_METHODS = frozenset({"get", "head"})
WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})


def classify_http_method(method: str) -> Literal["read", "write"]:
    """Classify an OpenAPI operation by conservative HTTP semantics.

    GET/HEAD are read candidates. Every other supported method is treated as a
    write by default, including OPTIONS/TRACE, because company connectors should
    expose only intentionally approved behavior to AI clients.
    """
    return "read" if method.strip().lower() in READ_METHODS else "write"


def read_only_operation_ids(document: dict[str, Any]) -> tuple[str, ...]:
    """Return operationIds that are safe to expose by default.

    Missing operationIds are excluded because FastMCP allowlisting is based on a
    stable operation id. This is the company-connector default: read operations
    are available, writes require an explicit allowlist.
    """
    ids: list[str] = []
    paths = document.get("paths", {})
    if not isinstance(paths, dict):
        return ()
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method in READ_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str) and operation_id:
                ids.append(operation_id)
    return tuple(ids)


def company_connector_operation_ids(
    document: dict[str, Any],
    *,
    allow_write_operation_ids: Collection[str] = (),
) -> tuple[str, ...]:
    """Return the default company connector allowlist.

    The default is all GET/HEAD operationIds plus only those write operationIds
    that were explicitly approved by the company/operator.
    """
    allowed_writes = frozenset(allow_write_operation_ids)
    ids: list[str] = []
    paths = document.get("paths", {})
    if not isinstance(paths, dict):
        return ()
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                continue
            if classify_http_method(method) == "read" or operation_id in allowed_writes:
                ids.append(operation_id)
    return tuple(ids)


class IntakeError(ValueError):
    """A safe, user-facing OpenAPI intake error."""


@dataclass(frozen=True)
class PreviewIssue:
    code: str
    severity: Literal["error", "warning"]
    message: str
    operation_id: str | None = None


@dataclass(frozen=True)
class InputFieldPreview:
    name: str
    location: str
    required: bool
    type: str
    default: Any = None
    enum: tuple[Any, ...] = ()


@dataclass(frozen=True)
class OperationPreview:
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    parameter_count: int
    has_request_body: bool
    auth_schemes: tuple[str, ...]
    risk: Literal["read", "write", "delete"]
    input_fields: tuple[InputFieldPreview, ...]


@dataclass(frozen=True)
class OpenAPIPreview:
    title: str
    version: str
    openapi_version: str
    operations: tuple[OperationPreview, ...]
    auth_schemes: tuple[str, ...]
    issues: tuple[PreviewIssue, ...]


def _decode_document(raw: bytes) -> dict[str, Any]:
    if not raw.strip():
        raise IntakeError("OpenAPI document is empty")

    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise IntakeError("Document is neither valid JSON nor safe YAML") from exc

    if not isinstance(document, dict):
        raise IntakeError("OpenAPI document must be an object")
    return document


def parse_openapi_text(text: str | bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > max_bytes:
        raise IntakeError(f"OpenAPI document exceeds {max_bytes} bytes")
    return _decode_document(raw)


def _security_scheme_names(
    document: dict[str, Any],
    operation: dict[str, Any],
) -> tuple[str, ...]:
    security = operation.get("security", document.get("security", []))
    if not isinstance(security, list):
        return ()
    names: set[str] = set()
    for requirement in security:
        if isinstance(requirement, dict):
            names.update(str(name) for name in requirement)
    return tuple(sorted(names))


def _resolve_local_ref(document: dict[str, Any], value: Any) -> Any:
    if not isinstance(value, dict) or "$ref" not in value:
        return value
    reference = value.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return value
    current: Any = document
    for part in reference[2:].split("/"):
        if not isinstance(current, dict):
            return value
        current = current.get(part.replace("~1", "/").replace("~0", "~"))
    return current if isinstance(current, dict) else value


def _schema_type(schema: dict[str, Any]) -> str:
    value = schema.get("type")
    if isinstance(value, str):
        return value
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict) and variant.get("type") not in {None, "null"}:
                return str(variant["type"])
    return "string"


def _input_fields(
    document: dict[str, Any],
    path_item: dict[str, Any],
    operation: dict[str, Any],
) -> tuple[InputFieldPreview, ...]:
    fields: list[InputFieldPreview] = []
    parameters = [
        *path_item.get("parameters", []),
        *operation.get("parameters", []),
    ]
    for raw_parameter in parameters:
        parameter = _resolve_local_ref(document, raw_parameter)
        if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str):
            continue
        schema = _resolve_local_ref(document, parameter.get("schema", {}))
        fields.append(
            InputFieldPreview(
                name=parameter["name"],
                location=str(parameter.get("in") or "query"),
                required=bool(parameter.get("required")),
                type=_schema_type(schema),
                default=schema.get("default"),
                enum=tuple(schema.get("enum", ())),
            )
        )

    request_body = _resolve_local_ref(document, operation.get("requestBody", {}))
    content = request_body.get("content", {}) if isinstance(request_body, dict) else {}
    media = content.get("application/json", {}) if isinstance(content, dict) else {}
    body_schema = _resolve_local_ref(document, media.get("schema", {}))
    properties = body_schema.get("properties", {}) if isinstance(body_schema, dict) else {}
    required_names = set(body_schema.get("required", ())) if isinstance(body_schema, dict) else set()
    if isinstance(properties, dict):
        for name, raw_schema in properties.items():
            schema = _resolve_local_ref(document, raw_schema)
            fields.append(
                InputFieldPreview(
                    name=str(name),
                    location="body",
                    required=name in required_names,
                    type=_schema_type(schema),
                    default=schema.get("default"),
                    enum=tuple(schema.get("enum", ())),
                )
            )
    return tuple(fields)


def build_openapi_preview(document: dict[str, Any]) -> OpenAPIPreview:
    openapi_version = document.get("openapi")
    if not isinstance(openapi_version, str) or not openapi_version.startswith("3."):
        raise IntakeError("Only OpenAPI 3.x documents are supported")

    info = document.get("info")
    paths = document.get("paths")
    if not isinstance(info, dict) or not isinstance(paths, dict):
        raise IntakeError("OpenAPI document must contain object fields 'info' and 'paths'")

    schemes = document.get("components", {}).get("securitySchemes", {})
    auth_schemes = tuple(sorted(schemes)) if isinstance(schemes, dict) else ()
    operations: list[OperationPreview] = []
    issues: list[PreviewIssue] = []

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_parameters = path_item.get("parameters", [])
        shared_count = len(shared_parameters) if isinstance(shared_parameters, list) else 0
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            operation_id = operation_id if isinstance(operation_id, str) and operation_id else None
            summary = operation.get("summary")
            summary = summary if isinstance(summary, str) and summary.strip() else None
            parameters = operation.get("parameters", [])
            parameter_count = shared_count + (len(parameters) if isinstance(parameters, list) else 0)

            operations.append(
                OperationPreview(
                    method=method.upper(),
                    path=str(path),
                    operation_id=operation_id,
                    summary=summary,
                    parameter_count=parameter_count,
                    has_request_body=isinstance(operation.get("requestBody"), dict),
                    auth_schemes=_security_scheme_names(document, operation),
                    risk=("read" if classify_http_method(method) == "read" else "delete" if method == "delete" else "write"),
                    input_fields=_input_fields(document, path_item, operation),
                )
            )
            if operation_id is None:
                issues.append(
                    PreviewIssue(
                        code="missing_operation_id",
                        severity="error",
                        message=f"{method.upper()} {path} has no operationId and cannot be safely allowlisted",
                    )
                )
            if summary is None and not operation.get("description"):
                issues.append(
                    PreviewIssue(
                        code="missing_description",
                        severity="warning",
                        message=f"{method.upper()} {path} has no summary or description",
                        operation_id=operation_id,
                    )
                )

    counts = Counter(operation.operation_id for operation in operations if operation.operation_id)
    for operation_id, count in counts.items():
        if count > 1:
            issues.append(
                PreviewIssue(
                    code="duplicate_operation_id",
                    severity="error",
                    message=f"operationId '{operation_id}' is used {count} times",
                    operation_id=operation_id,
                )
            )

    if not operations:
        issues.append(
            PreviewIssue(
                code="no_operations",
                severity="error",
                message="The document contains no supported HTTP operations",
            )
        )

    return OpenAPIPreview(
        title=str(info.get("title") or "Untitled API"),
        version=str(info.get("version") or "unknown"),
        openapi_version=openapi_version,
        operations=tuple(operations),
        auth_schemes=auth_schemes,
        issues=tuple(issues),
    )


async def resolve_public_host(hostname: str) -> tuple[str, ...]:
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise IntakeError("URL hostname could not be resolved") from exc
    return tuple(sorted({record[4][0] for record in records}))


def _validate_public_addresses(addresses: tuple[str, ...]) -> None:
    if not addresses:
        raise IntakeError("URL hostname resolved to no addresses")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise IntakeError("URL hostname resolved to an invalid address") from exc
        if not parsed.is_global:
            raise IntakeError("URL hostname resolves to a private or reserved address")


async def fetch_openapi_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = 10,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str], Awaitable[tuple[str, ...]]] = resolve_public_host,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise IntakeError("Only HTTP and HTTPS URLs are allowed")
    if not parsed.hostname:
        raise IntakeError("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise IntakeError("Credentials are not allowed in OpenAPI URLs")

    _validate_public_addresses(await resolver(parsed.hostname))

    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        ) as client:
            async with client.stream("GET", url, headers={"Accept": "application/json, application/yaml, text/yaml"}) as response:
                if response.is_redirect:
                    raise IntakeError("Redirects are not followed; provide the final OpenAPI URL")
                response.raise_for_status()
                declared_length = response.headers.get("content-length")
                if declared_length and int(declared_length) > max_bytes:
                    raise IntakeError(f"OpenAPI document exceeds {max_bytes} bytes")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise IntakeError(f"OpenAPI document exceeds {max_bytes} bytes")
                    chunks.append(chunk)
    except IntakeError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise IntakeError(f"Could not fetch OpenAPI URL: {exc}") from exc

    return parse_openapi_text(b"".join(chunks), max_bytes=max_bytes)
