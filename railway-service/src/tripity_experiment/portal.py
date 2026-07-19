"""Minimal self-service portal: account, personal connections, MCP URL."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import secrets
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from tripity_experiment.connection_store import Connection, ConnectionStore
from tripity_experiment.identity import IdentityStore, User
from tripity_experiment.personal_host import personal_mcp_url

PORTAL_FILE = Path(__file__).with_name("web").joinpath("portal.html")


class Credentials(BaseModel):
    username: str
    password: str = Field(min_length=8)


class NewConnection(BaseModel):
    name: str = Field(min_length=1)
    kind: Literal["openapi", "mcp", "browser"] = "openapi"
    openapi_url: str = ""
    base_url: str = ""
    operations: list[str] = Field(default_factory=list)
    mcp_url: str | None = None
    api_bearer_token: str | None = None
    # For kind="browser": the site root the AI gets full access to, run inside
    # the user's own Chrome session (the "connect this site" button).
    origin: str = ""


def create_portal_app(
    identities: IdentityStore,
    connections: ConnectionStore,
    *,
    public_base_url: str = "http://127.0.0.1:8000",
    registration_enabled: bool = True,
    cors_origins: list[str] | None = None,
    oauth_clients_path: str | Path | None = None,
    cloud_browser_login_url_for: Callable[[User], str] | None = None,
    cloud_browser_status_for: Callable[[User], dict[str, Any]] | None = None,
    cloud_browser_delete_for: Callable[[User], dict[str, Any]] | None = None,
    activity_log_path: str | Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Tripity")
    oauth_clients_file = Path(oauth_clients_path) if oauth_clients_path else None
    activity_file = Path(activity_log_path) if activity_log_path else None

    def _load_oauth_clients() -> dict[str, dict[str, Any]]:
        if oauth_clients_file is None or not oauth_clients_file.exists():
            return {}
        return json.loads(oauth_clients_file.read_text(encoding="utf-8"))

    def _save_oauth_clients(clients: dict[str, dict[str, Any]]) -> None:
        if oauth_clients_file is None:
            return
        oauth_clients_file.parent.mkdir(parents=True, exist_ok=True)
        oauth_clients_file.write_text(json.dumps(clients, indent=2), encoding="utf-8")

    oauth_clients: dict[str, dict[str, Any]] = _load_oauth_clients()
    oauth_codes: dict[str, dict[str, Any]] = {}

    # Allow the Next.js frontend (a different origin) to call this API. Auth is a
    # Bearer token (not cookies), so we don't need credentialed CORS.
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def current_user(authorization: str | None = Header(default=None)) -> User:
        prefix = "Bearer "
        token = authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else ""
        user = identities.authenticate(token) if token else None
        if user is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return user

    def current_actor(authorization: str | None = Header(default=None)) -> str:
        prefix = "Bearer "
        token = authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else ""
        if not token or identities.authenticate(token) is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return hashlib.sha256(("token:" + authorization).encode()).hexdigest()[:12]

    def _recent_activity_for_actor(actor: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if activity_file is None or not activity_file.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in activity_file.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("actor") != actor:
                continue
            records.append(
                {
                    "event": record.get("event"),
                    "method": record.get("method", ""),
                    "path": record.get("path", ""),
                    "status": record.get("status"),
                    "ts": record.get("ts"),
                    "latency_ms": record.get("latency_ms"),
                }
            )
        return records[-limit:][::-1]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return PORTAL_FILE.read_text(encoding="utf-8")

    def _oauth_issuer() -> str:
        return public_base_url.rstrip("/")

    def _oauth_user_for_resource(resource: str | None) -> User | None:
        if not resource:
            return None
        parsed = urlparse(resource)
        parts = [part for part in parsed.path.split("/") if part]
        public_id = parts[1] if len(parts) >= 2 and parts[0] == "mcp" else ""
        return next((user for user in identities.users() if user.public_id == public_id), None)

    def _pkce_s256(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @app.get("/.well-known/oauth-authorization-server")
    async def oauth_authorization_server() -> dict[str, Any]:
        issuer = _oauth_issuer()
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }

    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def oauth_protected_resource() -> dict[str, Any]:
        issuer = _oauth_issuer()
        return {
            "resource": f"{issuer}/mcp",
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
        }

    @app.get("/.well-known/oauth-protected-resource/mcp/{public_id}")
    async def oauth_protected_resource_for_user(public_id: str) -> dict[str, Any]:
        user = next((candidate for candidate in identities.users() if candidate.public_id == public_id), None)
        if user is None:
            raise HTTPException(status_code=404, detail="Unknown MCP resource")
        issuer = _oauth_issuer()
        return {
            "resource": personal_mcp_url(issuer, user),
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
        }

    @app.post("/oauth/register")
    async def oauth_register(body: dict[str, Any]) -> dict[str, Any]:
        client_id = secrets.token_urlsafe(18)
        oauth_clients[client_id] = {
            "client_id": client_id,
            "redirect_uris": body.get("redirect_uris") or [],
            "client_name": body.get("client_name") or "AI client",
        }
        _save_oauth_clients(oauth_clients)
        return {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": oauth_clients[client_id]["redirect_uris"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": oauth_clients[client_id]["client_name"],
        }

    @app.get("/oauth/authorize", response_class=HTMLResponse)
    async def oauth_authorize_form(request: Request) -> str:
        q = dict(request.query_params)
        client = oauth_clients.get(q.get("client_id", ""))
        resource_user = _oauth_user_for_resource(q.get("resource"))
        if client is None or resource_user is None:
            raise HTTPException(status_code=400, detail="Invalid OAuth request")
        hidden = "".join(
            f'<input type="hidden" name="{key}" value="{value}">'
            for key, value in q.items()
        )
        return f"""
<!doctype html><html><head><title>Authorize Tripity</title>
<style>body{{font-family:system-ui;margin:0;background:#f6f8fb;color:#12233b}}main{{max-width:480px;margin:60px auto;background:white;padding:28px;border-radius:16px;border:1px solid #e1e7ef}}input,button{{width:100%;box-sizing:border-box;padding:10px;margin-top:10px;border-radius:8px;border:1px solid #cbd5e1}}button{{background:#0f2a4a;color:white;font-weight:700;cursor:pointer}}</style>
</head><body><main><h1>Authorize Tripity</h1><p>Allow <b>{client['client_name']}</b> to use websites you authorize in Tripity.</p><p>Account: <b>{resource_user.username}</b></p><form method="post" action="/oauth/authorize">{hidden}<input name="username" placeholder="Email / username" autocomplete="username"><input name="password" type="password" placeholder="Password" autocomplete="current-password"><button>Allow</button></form></main></body></html>
"""

    @app.post("/oauth/authorize")
    async def oauth_authorize_submit(request: Request) -> HTMLResponse:
        form = parse_qs((await request.body()).decode())
        data = {key: values[0] for key, values in form.items()}
        client = oauth_clients.get(data.get("client_id", ""))
        resource_user = _oauth_user_for_resource(data.get("resource"))
        if client is None or resource_user is None:
            raise HTTPException(status_code=400, detail="Invalid OAuth request")
        try:
            login = identities.login(data.get("username", ""), data.get("password", ""))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Invalid credentials") from exc
        if login.user.id != resource_user.id:
            raise HTTPException(status_code=403, detail="Wrong Tripity account for this connection")
        redirect_uri = data.get("redirect_uri", "")
        if redirect_uri not in client["redirect_uris"]:
            raise HTTPException(status_code=400, detail="Invalid redirect_uri")
        code = secrets.token_urlsafe(24)
        oauth_codes[code] = {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "user_id": login.user.id,
            "code_challenge": data.get("code_challenge", ""),
            "expires_at": time.time() + 300,
        }
        location = redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(
            {"code": code, **({"state": data["state"]} if data.get("state") else {})}
        )
        return HTMLResponse("", status_code=302, headers={"Location": location})

    @app.post("/oauth/token")
    async def oauth_token(request: Request) -> dict[str, Any]:
        form = parse_qs((await request.body()).decode())
        data = {key: values[0] for key, values in form.items()}
        if data.get("grant_type") != "authorization_code":
            raise HTTPException(status_code=400, detail="Unsupported grant_type")
        code = data.get("code", "")
        record = oauth_codes.pop(code, None)
        if not record or record["expires_at"] < time.time():
            raise HTTPException(status_code=400, detail="Invalid code")
        if record["client_id"] != data.get("client_id") or record["redirect_uri"] != data.get("redirect_uri"):
            raise HTTPException(status_code=400, detail="Invalid OAuth client")
        verifier = data.get("code_verifier", "")
        if record.get("code_challenge") and _pkce_s256(verifier) != record["code_challenge"]:
            raise HTTPException(status_code=400, detail="Invalid PKCE verifier")
        login = identities.issue_token(record["user_id"])
        return {
            "access_token": login.access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "tripity:websites:read",
        }

    @app.post("/api/register")
    async def register(body: Credentials) -> dict[str, str]:
        if not registration_enabled:
            raise HTTPException(status_code=403, detail="Registration is closed for this pilot")
        try:
            user = identities.register(body.username, body.password)
            return {"id": user.id, "username": user.username}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/login")
    async def login(body: Credentials) -> dict[str, str]:
        try:
            session = identities.login(body.username, body.password)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {
            "access_token": session.access_token,
            "username": session.user.username,
            "mcp_url": personal_mcp_url(public_base_url, session.user),
        }

    @app.post("/api/start")
    async def start(body: Credentials) -> dict[str, str]:
        """One-step onboarding: register the user if new, then log in.

        Lets the extension get someone from install to a live MCP URL with a
        single email + password, no separate sign-up visit. Wrong password on an
        existing account still fails as 401 (we never leak which case it was by
        accident: existence is checked explicitly, not inferred from a login error).
        """
        username_cf = body.username.strip().casefold()
        exists = any(user.username == username_cf for user in identities.users())
        if not exists:
            if not registration_enabled:
                raise HTTPException(status_code=403, detail="Registration is closed for this pilot")
            try:
                identities.register(body.username, body.password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            session = identities.login(body.username, body.password)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {
            "access_token": session.access_token,
            "username": session.user.username,
            "mcp_url": personal_mcp_url(public_base_url, session.user),
        }

    @app.get("/api/me")
    async def me(user: User = Depends(current_user)) -> dict[str, str]:
        return {
            "id": user.id,
            "username": user.username,
            "mcp_url": personal_mcp_url(public_base_url, user),
        }

    @app.get("/api/activity")
    async def activity(
        _user: User = Depends(current_user),
        actor: str = Depends(current_actor),
    ) -> dict[str, Any]:
        """Return this session's recent redacted Tripity activity."""
        return {"items": _recent_activity_for_actor(actor)}

    @app.get("/api/cloud-browser")
    async def cloud_browser(user: User = Depends(current_user)) -> dict[str, Any]:
        if cloud_browser_login_url_for is None:
            return {"enabled": False}
        status = cloud_browser_status_for(user) if cloud_browser_status_for else {}
        return {"enabled": True, **status, "login_url": cloud_browser_login_url_for(user)}

    @app.delete("/api/cloud-browser")
    async def delete_cloud_browser(user: User = Depends(current_user)) -> dict[str, Any]:
        if cloud_browser_delete_for is None:
            raise HTTPException(status_code=404, detail="Cloud browser is not enabled")
        return cloud_browser_delete_for(user)

    @app.get("/api/connections")
    async def list_connections(user: User = Depends(current_user)) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "kind": item.kind,
                "operations": item.operations,
                "source": (
                    item.origin
                    if item.kind == "browser"
                    else item.openapi_url
                    if item.kind == "openapi"
                    else item.mcp_url
                ),
            }
            for item in connections.load(user.id)
        ]

    @app.post("/api/connect-garmin")
    async def connect_garmin(user: User = Depends(current_user)) -> dict[str, Any]:
        """One-button Garmin connection: create the browser connection and return next steps."""
        connection = Connection(
            name="garmin",
            owner_id=user.id,
            kind="browser",
            origin="https://connect.garmin.com",
        )
        connections.add(connection)
        cloud: dict[str, Any] = {"enabled": False}
        if cloud_browser_login_url_for is not None:
            status = cloud_browser_status_for(user) if cloud_browser_status_for else {}
            cloud = {"enabled": True, **status, "login_url": cloud_browser_login_url_for(user)}
        return {
            "connection": {"name": "garmin", "kind": "browser", "origin": connection.origin},
            "mcp_url": personal_mcp_url(public_base_url, user),
            "cloud_browser": cloud,
        }

    @app.post("/api/connections")
    async def add_connection(
        body: NewConnection,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        if body.kind == "openapi" and (
            not body.openapi_url or not body.base_url or not body.operations
        ):
            raise HTTPException(
                status_code=400,
                detail="OpenAPI URL, base URL and at least one operation are required",
            )
        if body.kind == "mcp" and not body.mcp_url:
            raise HTTPException(status_code=400, detail="MCP URL is required")
        if body.kind == "browser" and not body.origin:
            raise HTTPException(status_code=400, detail="Site address is required")
        connection = Connection(
            name=body.name,
            openapi_url=body.openapi_url,
            base_url=body.base_url,
            operations=body.operations,
            api_bearer_token=body.api_bearer_token,
            owner_id=user.id,
            kind=body.kind,
            mcp_url=body.mcp_url,
            origin=body.origin.rstrip("/") or None,
        )
        connections.add(connection)
        return {
            "name": connection.name,
            "operations": connection.operations,
            "mcp_url": personal_mcp_url(public_base_url, user),
        }

    @app.delete("/api/connections/{name}")
    async def delete_connection(
        name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        if not connections.remove(user.id, name):
            raise HTTPException(status_code=404, detail="Connection not found")
        return {"deleted": name}

    @app.delete("/api/account")
    async def delete_account(user: User = Depends(current_user)) -> dict[str, Any]:
        # Explicit account deletion: wipe the user's connections, then the user.
        removed = connections.remove_all(user.id)
        identities.delete(user.id)
        return {"deleted_user": user.username, "removed_connections": removed}

    return app


def main() -> None:
    import os

    import uvicorn

    encryption_key = os.getenv("TRIPITY_ENCRYPTION_KEY")
    if not encryption_key:
        raise RuntimeError("TRIPITY_ENCRYPTION_KEY is required")
    app = create_portal_app(
        IdentityStore(os.getenv("TRIPITY_USERS", "tripity_users.json")),
        ConnectionStore(
            os.getenv("TRIPITY_STORE", "tripity_connections.json"),
            encryption_key=encryption_key,
        ),
        public_base_url=os.getenv("TRIPITY_PUBLIC_URL", "http://127.0.0.1:8000"),
        registration_enabled=os.getenv("TRIPITY_REGISTRATION_ENABLED", "true").lower()
        in {"1", "true", "yes"},
        cors_origins=[
            o.strip()
            for o in os.getenv(
                "TRIPITY_CORS_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000",
            ).split(",")
            if o.strip()
        ],
    )
    uvicorn.run(
        app,
        host=os.getenv("TRIPITY_HOST", "127.0.0.1"),
        port=int(os.getenv("TRIPITY_PORT", "8080")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
