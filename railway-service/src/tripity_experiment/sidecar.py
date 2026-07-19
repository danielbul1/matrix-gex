from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from tripity_experiment.mcp_adapter import build_mcp_server
from tripity_experiment.openapi_intake import parse_openapi_text
from tripity_experiment.project_gateway import BearerGate


class SidecarApp:
    def __init__(self, mcp_app: Any) -> None:
        self.mcp_app = mcp_app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path") == "/health":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"status":"ok"}',
                }
            )
            return
        await self.mcp_app(scope, receive, send)


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_spec() -> dict[str, Any]:
    spec_file = os.getenv("TRIPITY_OPENAPI_FILE")
    spec_url = os.getenv("TRIPITY_OPENAPI_URL")
    if bool(spec_file) == bool(spec_url):
        raise RuntimeError(
            "Set exactly one of TRIPITY_OPENAPI_FILE or TRIPITY_OPENAPI_URL"
        )
    if spec_file:
        return parse_openapi_text(Path(spec_file).read_bytes())
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.get(str(spec_url))
        response.raise_for_status()
        return parse_openapi_text(response.content)


def _oauth_enabled() -> bool:
    return os.getenv("TRIPITY_OAUTH_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _build_oauth_provider() -> Any:
    """Build an OAuth provider so cloud AI surfaces (e.g. ChatGPT) can connect.

    Uses FastMCP's in-memory OAuth provider, which speaks a full OAuth 2.1 flow
    (discovery, dynamic client registration, PKCE) with no external calls. When
    TRIPITY_OAUTH_STATE_FILE is set, clients and tokens persist to that file
    (encrypted with TRIPITY_ENCRYPTION_KEY) so connections survive restarts.
    For production this provider is swappable for a real IdP (FastMCP ships
    Auth0, WorkOS, Google, etc. providers) without touching the rest of the
    sidecar.
    """
    from mcp.server.auth.settings import ClientRegistrationOptions

    from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

    public_url = os.getenv(
        "TRIPITY_PUBLIC_URL", f"http://localhost:{os.getenv('TRIPITY_PORT', '8000')}"
    )
    state_file = os.getenv("TRIPITY_OAUTH_STATE_FILE")
    if state_file:
        from tripity_experiment.oauth_state import PersistentOAuthProvider

        return PersistentOAuthProvider(
            state_path=state_file,
            encryption_key=_required("TRIPITY_ENCRYPTION_KEY"),
            base_url=public_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
    return InMemoryOAuthProvider(
        base_url=public_url,
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )


def create_sidecar_app() -> SidecarApp:
    allowed = [
        item.strip()
        for item in _required("TRIPITY_ALLOWED_OPERATIONS").split(",")
        if item.strip()
    ]
    if not allowed:
        raise RuntimeError("TRIPITY_ALLOWED_OPERATIONS cannot be empty")
    oauth_enabled = _oauth_enabled()
    mcp = build_mcp_server(
        openapi_spec=load_spec(),
        base_url=_required("TRIPITY_API_BASE_URL"),
        allowed_operation_ids=allowed,
        bearer_token=os.getenv("TRIPITY_UPSTREAM_BEARER_TOKEN"),
        auth=_build_oauth_provider() if oauth_enabled else None,
    )
    app: Any = mcp.http_app(path="/mcp", stateless_http=True)
    client_token = os.getenv("TRIPITY_CLIENT_BEARER_TOKEN")
    # OAuth and the static Bearer gate are alternative auth modes; OAuth wins.
    if client_token and not oauth_enabled:
        app = BearerGate(app, client_token)
    return SidecarApp(app)


def main() -> None:
    uvicorn.run(
        "tripity_experiment.sidecar:create_sidecar_app",
        factory=True,
        host=os.getenv("TRIPITY_HOST", "0.0.0.0"),
        port=int(os.getenv("TRIPITY_PORT", "8000")),
        log_level=os.getenv("TRIPITY_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
