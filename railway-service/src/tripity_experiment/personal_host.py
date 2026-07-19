"""One hosted service exposing a stable private MCP URL per Tripity user."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import asyncio
import secrets
from typing import Any

from starlette.applications import Starlette
from starlette._utils import get_route_path
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from tripity_experiment.identity import IdentityStore, User
from tripity_experiment.personal_gateway import PersonalGatewayFactory


def oauth_www_authenticate(base_url: str | None = None, public_id: str | None = None) -> str:
    if not base_url:
        return "Bearer"
    suffix = f"/mcp/{public_id}" if public_id else "/mcp"
    metadata = f"{base_url.rstrip('/')}/.well-known/oauth-protected-resource{suffix}"
    return f'Bearer resource_metadata="{metadata}"'


class OwnerBearerGate:
    """Require a valid token belonging to the owner of this mounted gateway."""

    def __init__(
        self,
        app: Any,
        identities: IdentityStore,
        owner_id: str,
        *,
        public_base_url: str | None = None,
    ) -> None:
        self.app = app
        self.identities = identities
        self.owner_id = owner_id
        self.public_base_url = public_base_url

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            supplied = headers.get(b"authorization", b"")
            prefix = b"Bearer "
            token = supplied[len(prefix) :].decode() if supplied.startswith(prefix) else ""
            user = self.identities.authenticate(token) if token else None
            if user is None or not secrets.compare_digest(user.id, self.owner_id):
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": oauth_www_authenticate(self.public_base_url)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class DynamicPersonalHost:
    """Resolve a personal gateway on each request so new users work immediately."""

    def __init__(
        self,
        identities: IdentityStore,
        factory: PersonalGatewayFactory,
        *,
        public_base_url: str | None = None,
    ) -> None:
        self.identities = identities
        self.factory = factory
        self.public_base_url = public_base_url
        self._children: dict[str, tuple[int, Any]] = {}
        self._lifespans: list[Any] = []
        self._lock = asyncio.Lock()

    def _connection_version(self) -> int:
        path = self.factory.connections.path
        return path.stat().st_mtime_ns if path.exists() else 0

    async def _child_for(self, user: User) -> Any:
        version = self._connection_version()
        cached = self._children.get(user.id)
        if cached and cached[0] == version:
            return cached[1]
        async with self._lock:
            cached = self._children.get(user.id)
            if cached and cached[0] == version:
                return cached[1]
            personal = await self.factory.create_for_user(user)
            child = personal.gateway.http_app(path="/", stateless_http=True)
            lifespan = child.router.lifespan_context(child)
            await lifespan.__aenter__()
            self._lifespans.append(lifespan)
            self._children[user.id] = (version, child)
            return child

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            response = JSONResponse({"error": "unsupported"}, status_code=400)
            await response(scope, receive, send)
            return

        public_id, separator, _remainder = get_route_path(scope).lstrip("/").partition("/")
        user = next(
            (candidate for candidate in self.identities.users() if candidate.public_id == public_id),
            None,
        )
        if user is None or not separator:
            response = JSONResponse({"error": "not found"}, status_code=404)
            await response(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"")
        prefix = b"Bearer "
        token = supplied[len(prefix) :].decode() if supplied.startswith(prefix) else ""
        authenticated = self.identities.authenticate(token) if token else None
        if authenticated is None or not secrets.compare_digest(authenticated.id, user.id):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": oauth_www_authenticate(self.public_base_url, user.public_id)},
            )
            await response(scope, receive, send)
            return

        child = await self._child_for(user)
        child_scope = dict(scope)
        child_scope["root_path"] = (
            f"{scope.get('root_path', '').rstrip('/')}/{public_id}"
        )
        await child(child_scope, receive, send)


def personal_mcp_url(base_url: str, user: User) -> str:
    return f"{base_url.rstrip('/')}/mcp/{user.public_id}/"


async def build_personal_host(
    identities: IdentityStore,
    factory: PersonalGatewayFactory,
) -> Starlette:
    """Build all personal gateways and host them below stable public IDs."""
    children: list[Any] = []
    routes: list[Any] = []
    for user in identities.users():
        personal = await factory.create_for_user(user)
        child = personal.gateway.http_app(path="/", stateless_http=True)
        children.append(child)
        routes.append(
            Mount(
                f"/mcp/{user.public_id}",
                app=OwnerBearerGate(child, identities, user.id),
            )
        )

    async def health(_request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "users": len(children)})

    routes.append(Route("/health", health))

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with AsyncExitStack() as stack:
            for child in children:
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    import asyncio
    import os

    import uvicorn

    encryption_key = os.getenv("TRIPITY_ENCRYPTION_KEY")
    if not encryption_key:
        raise RuntimeError("TRIPITY_ENCRYPTION_KEY is required")

    from tripity_experiment.connection_store import ConnectionStore

    identities = IdentityStore(os.getenv("TRIPITY_USERS", "tripity_users.json"))
    connections = ConnectionStore(
        os.getenv("TRIPITY_STORE", "tripity_connections.json"),
        encryption_key=encryption_key,
    )
    factory = PersonalGatewayFactory(identities, connections)
    app = asyncio.run(build_personal_host(identities, factory))
    from tripity_experiment.safety import ActivityAndRateLimit

    app = ActivityAndRateLimit(
        app,
        log_path=os.getenv("TRIPITY_ACTIVITY_LOG", "tripity_activity.jsonl"),
        limit=int(os.getenv("TRIPITY_RATE_LIMIT", "120")),
    )
    uvicorn.run(
        app,
        host=os.getenv("TRIPITY_HOST", "127.0.0.1"),
        port=int(os.getenv("TRIPITY_PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
