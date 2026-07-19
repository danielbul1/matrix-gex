"""Onboarding CLI shipped inside the sidecar image.

Two commands close the friction recorded in loop 8:

- ``list-operations`` shows what the API can do in plain language, including the
  exact value to paste into ``TRIPITY_ALLOWED_OPERATIONS`` — so the user never
  needs to know what an "operationId" is.
- ``call`` runs a tool through a running sidecar and prints the result — so the
  user gets a first successful tool call without writing an MCP client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from tripity_experiment.openapi_intake import (
    IntakeError,
    OpenAPIPreview,
    build_openapi_preview,
    parse_openapi_text,
)

_RISK_LABEL = {"read": "reads data", "write": "changes data", "delete": "deletes data"}


def _load_spec(file: str | None, url: str | None) -> dict[str, Any]:
    file = file or os.getenv("TRIPITY_OPENAPI_FILE")
    url = url or os.getenv("TRIPITY_OPENAPI_URL")
    if bool(file) == bool(url):
        raise IntakeError(
            "Provide exactly one spec source: --file/TRIPITY_OPENAPI_FILE "
            "or --url/TRIPITY_OPENAPI_URL"
        )
    if file:
        return parse_openapi_text(Path(file).read_bytes())
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.get(str(url))
        response.raise_for_status()
        return parse_openapi_text(response.content)


def _format_operations(preview: OpenAPIPreview) -> str:
    lines: list[str] = []
    lines.append(f"API: {preview.title} (version {preview.version})")
    lines.append("")
    lines.append("Available operations:")
    lines.append("")

    allowlistable: list[str] = []
    for op in preview.operations:
        label = op.summary or f"{op.method} {op.path}"
        lines.append(f"  • {label}")
        lines.append(f"      what it does : {_RISK_LABEL.get(op.risk, op.risk)}  ({op.method} {op.path})")
        if op.operation_id:
            lines.append(f"      allow value  : {op.operation_id}")
            allowlistable.append(op.operation_id)
        else:
            lines.append("      allow value  : (unavailable — this operation has no id and cannot be selected)")
        required = [f.name for f in op.input_fields if f.required]
        if required:
            lines.append(f"      you provide  : {', '.join(required)}")
        lines.append("")

    if allowlistable:
        lines.append("To expose operations, set (comma-separated):")
        lines.append(f"  TRIPITY_ALLOWED_OPERATIONS={','.join(allowlistable)}")
        lines.append("Keep only the ones you actually want the AI to use.")
    else:
        lines.append("No operations can be selected: none of them declare an id.")

    warnings = [i for i in preview.issues if i.severity == "error"]
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for issue in warnings:
            lines.append(f"  ! {issue.message}")
    return "\n".join(lines)


def _cmd_list_operations(args: argparse.Namespace) -> int:
    try:
        spec = _load_spec(args.file, args.url)
        preview = build_openapi_preview(spec)
    except (IntakeError, httpx.HTTPError, OSError) as exc:
        print(f"Could not read the OpenAPI spec: {exc}", file=sys.stderr)
        return 1
    print(_format_operations(preview))
    return 0


async def _run_call(url: str, token: str | None, tool: str | None, payload: dict[str, Any]) -> int:
    # Imported lazily so `list-operations` works even without the MCP client stack.
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    auth = BearerAuth(token) if token else None
    async with Client(url, auth=auth) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools]
        if tool is None:
            print("Tools exposed by the sidecar:")
            for name in names:
                print(f"  • {name}")
            print("\nRun a tool with:  tripity call <tool> --json '{...}'")
            return 0
        if tool not in names:
            print(f"Tool '{tool}' is not exposed. Available: {', '.join(names) or '(none)'}", file=sys.stderr)
            return 1
        result = await client.call_tool(tool, payload)
        print(json.dumps(result.structured_content, indent=2, ensure_ascii=False))
        return 0


def _cmd_call(args: argparse.Namespace) -> int:
    url = args.sidecar_url or os.getenv("TRIPITY_SIDECAR_URL", "http://127.0.0.1:8000")
    if not url.rstrip("/").endswith("/mcp"):
        url = url.rstrip("/") + "/mcp"
    token = args.token or os.getenv("TRIPITY_CLIENT_BEARER_TOKEN")
    try:
        payload = json.loads(args.json) if args.json else {}
    except json.JSONDecodeError as exc:
        print(f"--json is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("--json must be a JSON object of tool arguments", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_run_call(url, token, args.tool, payload))
    except Exception as exc:  # noqa: BLE001 - surface a clean message to a non-technical user
        print(f"Tool call failed: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tripity",
        description="Connect an existing API to an AI, without writing code.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    lo = sub.add_parser(
        "list-operations",
        help="Show the API's operations in plain language and the values to allow.",
    )
    lo.add_argument("--file", help="Path to an OpenAPI file (defaults to TRIPITY_OPENAPI_FILE).")
    lo.add_argument("--url", help="URL of an OpenAPI document (defaults to TRIPITY_OPENAPI_URL).")
    lo.set_defaults(func=_cmd_list_operations)

    call = sub.add_parser(
        "call",
        help="Run a tool through a running sidecar and print the result.",
    )
    call.add_argument("tool", nargs="?", help="Tool name. Omit to list exposed tools.")
    call.add_argument("--json", help="Tool arguments as a JSON object, e.g. '{\"petId\": 1}'.")
    call.add_argument("--sidecar-url", help="Sidecar base URL (defaults to TRIPITY_SIDECAR_URL or http://127.0.0.1:8000).")
    call.add_argument("--token", help="Client bearer token (defaults to TRIPITY_CLIENT_BEARER_TOKEN).")
    call.set_defaults(func=_cmd_call)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
