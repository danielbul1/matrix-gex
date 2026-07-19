from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from tripity_experiment.company_connector import build_company_connector_draft
from tripity_experiment.company_delivery import build_company_delivery_packet
from tripity_experiment.company_export import build_company_export_text
from tripity_experiment.connector_manifest import ConnectorManifest, manifest_from_draft
from tripity_experiment.connector_runtime import ManifestMcpPublisher, PublishedConnector
from tripity_experiment.fixture_api import FIXTURE_TOKEN, app as fixture_app
from tripity_experiment.openapi_intake import fetch_openapi_url


class CreateCompanyConnectorRequest(BaseModel):
    company_name: str = Field(min_length=1)
    openapi_spec: dict[str, Any]
    api_base_url: str = Field(min_length=1)
    public_mcp_base_url: str = "https://mcp.tripity.ai"
    allow_write_operation_ids: list[str] = Field(default_factory=list)
    upstream_bearer_token: str | None = None


class CreateFromUrlRequest(BaseModel):
    company_name: str = Field(min_length=1)
    openapi_url: str = Field(min_length=1)
    api_base_url: str | None = None
    public_mcp_base_url: str = "https://mcp.tripity.ai"
    allow_write_operation_ids: list[str] = Field(default_factory=list)
    upstream_bearer_token: str | None = None


def infer_api_base_url(spec: dict[str, Any], openapi_url: str) -> str:
    """Best-effort API base URL from an OpenAPI spec's ``servers`` block.

    Absolute server URL -> used as-is; relative server URL -> joined with the
    OpenAPI URL origin; missing/invalid -> the OpenAPI URL origin.
    """

    parsed = urlsplit(openapi_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        url = servers[0].get("url")
        if isinstance(url, str) and url.strip():
            url = url.strip()
            if url.startswith(("http://", "https://")):
                return url
            return f"{origin}/{url.lstrip('/')}" if not url.startswith("/") else f"{origin}{url}"
    return origin


class TestReadRequest(BaseModel):
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


@dataclass
class StoredCompanyConnector:
    manifest: ConnectorManifest
    published: PublishedConnector
    openapi_spec: dict[str, Any]
    upstream_bearer_token: str | None = None


class CompanyConnectorRegistry:
    def __init__(self, publisher: ManifestMcpPublisher | None = None, storage_dir: str | Path | None = None) -> None:
        self.publisher = publisher or ManifestMcpPublisher()
        self.connectors: dict[str, StoredCompanyConnector] = {}
        self.storage_dir = Path(storage_dir) if storage_dir is not None else None
        self._fernet: Fernet | None = None
        if self.storage_dir is not None:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self._fernet = Fernet(self._load_or_create_key())

    def _key_path(self) -> Path:
        if self.storage_dir is None:
            raise RuntimeError("Connector storage is not configured")
        return self.storage_dir / "fernet.key"

    def _load_or_create_key(self) -> bytes:
        path = self._key_path()
        if path.exists():
            return path.read_bytes()
        key = Fernet.generate_key()
        path.write_bytes(key)
        return key

    def _encrypt_secret(self, value: str | None) -> str | None:
        if value is None:
            return None
        if self._fernet is None:
            raise RuntimeError("Connector secret encryption is not configured")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt_secret(self, value: str | None) -> str | None:
        if value is None:
            return None
        if self._fernet is None:
            raise RuntimeError("Connector secret encryption is not configured")
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")

    def _record_path(self, slug: str) -> Path:
        if self.storage_dir is None:
            raise RuntimeError("Connector storage is not configured")
        return self.storage_dir / f"{slug}.json"

    def save(self, stored: StoredCompanyConnector) -> None:
        if self.storage_dir is None:
            return
        self._record_path(stored.manifest.company.slug).write_text(
            json.dumps(
                {
                    "manifest_json": stored.manifest.to_json(),
                    "openapi_spec": stored.openapi_spec,
                    "encrypted_client_token": self._encrypt_secret(stored.published.access_token),
                    "encrypted_upstream_bearer_token": self._encrypt_secret(stored.upstream_bearer_token),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def restore(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if self.storage_dir is None:
            return
        for path in sorted(self.storage_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            manifest = ConnectorManifest.from_json(data["manifest_json"])
            openapi_spec = data["openapi_spec"]
            upstream_bearer_token = self._decrypt_secret(data.get("encrypted_upstream_bearer_token"))
            published = self.publisher.publish(
                manifest=manifest,
                openapi_spec=openapi_spec,
                upstream_bearer_token=upstream_bearer_token,
                transport=transport,
                client_token=self._decrypt_secret(data.get("encrypted_client_token")),
            )
            self.connectors[manifest.company.slug] = StoredCompanyConnector(
                manifest=manifest,
                published=published,
                openapi_spec=openapi_spec,
                upstream_bearer_token=upstream_bearer_token,
            )

    def close(self) -> None:
        self.publisher.close()
        self.connectors.clear()


def create_company_connector_api(
    *,
    registry: CompanyConnectorRegistry | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    storage_dir: str | Path | None = None,
    spec_fetcher: Any = fetch_openapi_url,
    operator_token: str | None = None,
) -> FastAPI:
    state = registry or CompanyConnectorRegistry(storage_dir=storage_dir)
    state.restore(transport=upstream_transport)
    operator_token = operator_token or secrets.token_urlsafe(24)

    def require_operator(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {operator_token}":
            raise HTTPException(status_code=401, detail="Operator authentication required")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            state.close()

    app = FastAPI(title="Tripity Company Connector API", lifespan=lifespan)
    app.state.operator_token = operator_token

    @app.get("/company-demo", response_class=HTMLResponse)
    async def company_demo() -> HTMLResponse:
        html = Path(__file__).with_name("web").joinpath("company_demo.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    def _create_from_request(
        request: CreateCompanyConnectorRequest,
        *,
        transport: httpx.AsyncBaseTransport | None,
    ) -> dict[str, Any]:
        try:
            draft = build_company_connector_draft(
                company_name=request.company_name,
                openapi_spec=request.openapi_spec,
                api_base_url=request.api_base_url,
                public_mcp_base_url=request.public_mcp_base_url,
                allow_write_operation_ids=set(request.allow_write_operation_ids),
            )
            manifest = manifest_from_draft(draft)
            published = state.publisher.publish(
                manifest=manifest,
                openapi_spec=request.openapi_spec,
                upstream_bearer_token=request.upstream_bearer_token,
                transport=transport,
            )
        except Exception as exc:  # noqa: BLE001 - convert builder errors to API errors
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        stored = StoredCompanyConnector(
            manifest=manifest,
            published=published,
            openapi_spec=request.openapi_spec,
            upstream_bearer_token=request.upstream_bearer_token,
        )
        state.connectors[manifest.company.slug] = stored
        state.save(stored)
        return build_company_delivery_packet(manifest=manifest, published=published).to_dict()

    @app.post("/api/company-connectors/sample")
    async def create_sample_connector() -> dict[str, Any]:
        return _create_from_request(
            CreateCompanyConnectorRequest(
                company_name="Acme CRM Demo",
                openapi_spec=fixture_app.openapi(),
                api_base_url="http://fixture.test",
                public_mcp_base_url="https://mcp.tripity.ai",
                upstream_bearer_token=FIXTURE_TOKEN,
            ),
            transport=httpx.ASGITransport(app=fixture_app),
        )

    @app.post("/api/company-connectors/sample-petstore")
    async def create_petstore_sample_connector() -> dict[str, Any]:
        spec = await spec_fetcher("https://petstore3.swagger.io/api/v3/openapi.json")
        return _create_from_request(
            CreateCompanyConnectorRequest(
                company_name="Swagger Petstore Demo",
                openapi_spec=spec,
                api_base_url="https://petstore3.swagger.io/api/v3",
                public_mcp_base_url="https://mcp.tripity.ai",
            ),
            transport=upstream_transport,
        )

    @app.post("/api/company-connectors")
    async def create_connector(request: CreateCompanyConnectorRequest) -> dict[str, Any]:
        return _create_from_request(request, transport=upstream_transport)

    @app.post("/api/company-connectors/from-url")
    async def create_connector_from_url(request: CreateFromUrlRequest) -> dict[str, Any]:
        try:
            spec = await spec_fetcher(request.openapi_url)
        except Exception as exc:  # noqa: BLE001 - surface fetch problems as clean 400s
            raise HTTPException(status_code=400, detail=f"Could not fetch OpenAPI URL: {exc}") from exc
        api_base_url = request.api_base_url or infer_api_base_url(spec, request.openapi_url)
        return _create_from_request(
            CreateCompanyConnectorRequest(
                company_name=request.company_name,
                openapi_spec=spec,
                api_base_url=api_base_url,
                public_mcp_base_url=request.public_mcp_base_url,
                allow_write_operation_ids=request.allow_write_operation_ids,
                upstream_bearer_token=request.upstream_bearer_token,
            ),
            transport=upstream_transport,
        )

    @app.get("/api/company-connectors/{slug}/export", response_class=PlainTextResponse)
    async def export_connector(slug: str) -> PlainTextResponse:
        stored = state.connectors.get(slug)
        if stored is None:
            raise HTTPException(status_code=404, detail="Connector not found")
        packet = build_company_delivery_packet(
            manifest=stored.manifest,
            published=stored.published,
        )
        return PlainTextResponse(build_company_export_text(packet))

    @app.post("/api/company-connectors/{slug}/test-read")
    async def test_read_tool(slug: str, request: TestReadRequest | None = None) -> dict[str, Any]:
        stored = state.connectors.get(slug)
        if stored is None:
            raise HTTPException(status_code=404, detail="Connector not found")
        tool_name = (request.tool_name if request else None) or "get_item"
        arguments = (request.arguments if request else {}) or {"item_id": 7, "include_metadata": True}
        if tool_name not in stored.published.operation_ids:
            raise HTTPException(status_code=400, detail=f"Read tool {tool_name} is not enabled")
        enabled_tool = next((tool for tool in stored.manifest.tools if tool.name == tool_name and tool.enabled), None)
        if enabled_tool is None or enabled_tool.risk != "read":
            raise HTTPException(status_code=400, detail=f"Tool {tool_name} is not an enabled read tool")
        async with Client(
            stored.published.local_mcp_url,
            auth=BearerAuth(stored.published.access_token),
        ) as client:
            result = await client.call_tool(tool_name, arguments)
        return {
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result.structured_content,
        }

    @app.get("/api/company-connectors/{slug}/approvals", dependencies=[Depends(require_operator)])
    async def list_pending_approvals(slug: str) -> dict[str, Any]:
        stored = state.connectors.get(slug)
        if stored is None:
            raise HTTPException(status_code=404, detail="Connector not found")
        return {
            "pending": [
                {
                    "approval_id": p.approval_id,
                    "tool_name": p.tool_name,
                    "method": p.method,
                    "path": p.path,
                    "created_at": p.created_at,
                }
                for p in stored.published.pending_approvals
            ]
        }

    @app.post("/api/company-connectors/{slug}/approvals/{approval_id}/approve", dependencies=[Depends(require_operator)])
    async def approve_pending_write(slug: str, approval_id: str) -> dict[str, Any]:
        stored = state.connectors.get(slug)
        if stored is None:
            raise HTTPException(status_code=404, detail="Connector not found")
        try:
            status_code = await stored.published.approve(approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Approval not found") from exc
        return {"executed": True, "status_code": status_code, "approval_id": approval_id}

    @app.get("/api/company-connectors/{slug}")
    async def get_connector(slug: str) -> dict[str, Any]:
        stored = state.connectors.get(slug)
        if stored is None:
            raise HTTPException(status_code=404, detail="Connector not found")
        return build_company_delivery_packet(
            manifest=stored.manifest,
            published=stored.published,
        ).to_dict()

    app.state.company_connector_registry = state
    return app
