from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastmcp import Client
from pydantic import BaseModel, Field

from tripity_experiment.connection_kit import build_connection_kit
from tripity_experiment.mcp_adapter import build_mcp_server
from tripity_experiment.openapi_intake import (
    IntakeError,
    build_openapi_preview,
    fetch_openapi_url,
    parse_openapi_text,
    resolve_public_host,
)
from tripity_experiment.project_gateway import TemporaryProjectManager

INDEX_FILE = Path(__file__).with_name("web").joinpath("index.html")

project_manager = TemporaryProjectManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    project_manager.close()


app = FastAPI(title="Tripity Loop 3", version="0.1.0", lifespan=lifespan)

class SourceRequest(BaseModel):
    source_type: Literal["url", "text"]
    source: str = Field(min_length=1)


class TestRequest(SourceRequest):
    selected_operation_ids: list[str] = Field(min_length=1)
    operation_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProjectRequest(SourceRequest):
    selected_operation_ids: list[str] = Field(min_length=1)


class ConnectionKitRequest(ProjectRequest):
    auth: Literal["oauth", "bearer", "none"] = "oauth"


async def load_document(request: SourceRequest) -> dict[str, Any]:
    if request.source_type == "url":
        return await fetch_openapi_url(request.source)
    return parse_openapi_text(request.source)


async def public_base_url(
    document: dict[str, Any],
    *,
    source_url: str | None = None,
) -> str:
    servers = document.get("servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], dict):
        raise IntakeError("The OpenAPI document must provide a public servers[0].url")
    base_url = servers[0].get("url")
    if not isinstance(base_url, str) or "{" in base_url:
        raise IntakeError("The first server URL must contain no variables")
    from urllib.parse import urljoin, urlsplit

    if source_url:
        base_url = urljoin(source_url, base_url)
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IntakeError("The first server URL must be public HTTP or HTTPS")
    addresses = await resolve_public_host(parsed.hostname)
    from tripity_experiment.openapi_intake import _validate_public_addresses

    _validate_public_addresses(addresses)
    return base_url


async def run_tool_test(
    document: dict[str, Any],
    selected_operation_ids: list[str],
    operation_id: str,
    arguments: dict[str, Any],
    *,
    base_url: str | None = None,
    source_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    if operation_id not in selected_operation_ids:
        raise IntakeError("The tested operation must be selected")
    resolved_base_url = base_url or await public_base_url(
        document,
        source_url=source_url,
    )
    server = build_mcp_server(
        openapi_spec=document,
        base_url=resolved_base_url,
        allowed_operation_ids=selected_operation_ids,
        transport=transport,
        bearer_token=bearer_token,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            operation_id,
            arguments,
            raise_on_error=False,
        )
    return {
        "ok": not result.is_error,
        "content": [item.model_dump(mode="json") for item in result.content],
        "structured_content": result.structured_content,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_FILE.read_text(encoding="utf-8")


@app.post("/api/preview")
async def preview(request: SourceRequest) -> dict[str, Any]:
    try:
        document = await load_document(request)
        result = build_openapi_preview(document)
        return asdict(result)
    except IntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/test")
async def test_tool(request: TestRequest) -> dict[str, Any]:
    try:
        document = await load_document(request)
        return await run_tool_test(
            document,
            request.selected_operation_ids,
            request.operation_id,
            request.arguments,
            source_url=request.source if request.source_type == "url" else None,
        )
    except IntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/connection-kit")
async def connection_kit(request: ConnectionKitRequest) -> dict[str, Any]:
    """Produce the real, deployable sidecar config + steps to connect an AI."""
    try:
        document = await load_document(request)
        base_url = await public_base_url(
            document,
            source_url=request.source if request.source_type == "url" else None,
        )
        return build_connection_kit(
            source_type=request.source_type,
            source=request.source,
            api_base_url=base_url,
            selected_operation_ids=request.selected_operation_ids,
            auth=request.auth,
        )
    except IntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects")
async def create_project(request: ProjectRequest) -> dict[str, Any]:
    import asyncio

    try:
        document = await load_document(request)
        base_url = await public_base_url(
            document,
            source_url=request.source if request.source_type == "url" else None,
        )
        project = await asyncio.to_thread(
            project_manager.create,
            openapi_spec=document,
            base_url=base_url,
            operation_ids=request.selected_operation_ids,
        )
        return {
            "project_id": project.project_id,
            "mcp_url": project.mcp_url,
            "access_token": project.access_token,
            "authorization_header": f"Bearer {project.access_token}",
        }
    except IntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
