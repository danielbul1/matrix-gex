"""Production entry point: portal API and dynamic personal MCP on one service."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
import os
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tripity_experiment.cloud_browser import CloudBrowserManager, CloudBrowserRuntime
from tripity_experiment.connection_store import ConnectionStore
from tripity_experiment.docker_browser import DockerCloudBrowserOrchestrator
from tripity_experiment.identity import IdentityStore
from tripity_experiment.personal_gateway import PersonalGatewayFactory
from tripity_experiment.personal_host import DynamicPersonalHost
from tripity_experiment.portal import create_portal_app
from tripity_experiment.relay import (
    ExtensionRegistry,
    RelayExecutor,
    extension_websocket_endpoint,
)
from tripity_experiment.safety import ActivityAndRateLimit


def _env_enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


async def _idle_hibernation_scheduler(
    identities: IdentityStore,
    docker_orchestrator: DockerCloudBrowserOrchestrator,
    *,
    interval_seconds: float,
    max_idle_seconds: float,
) -> None:
    """Periodically stop idle Docker browser stacks while preserving profiles."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            docker_orchestrator.hibernate_idle(
                identities.users(), max_idle_seconds=max_idle_seconds
            )
        except Exception:
            # A scheduler failure must not take down Tripity. The next tick can retry.
            continue


def create_production_app() -> Starlette:
    encryption_key = os.getenv("TRIPITY_ENCRYPTION_KEY")
    if not encryption_key:
        raise RuntimeError("TRIPITY_ENCRYPTION_KEY is required")

    identities = IdentityStore(os.getenv("TRIPITY_USERS", "/data/tripity_users.json"))
    connections = ConnectionStore(
        os.getenv("TRIPITY_STORE", "/data/tripity_connections.json"),
        encryption_key=encryption_key,
    )
    public_base_url = os.getenv("TRIPITY_PUBLIC_URL", "http://127.0.0.1:8080")
    activity_log_path = os.getenv("TRIPITY_ACTIVITY_LOG", "/data/tripity_activity.jsonl")
    # Browser connections can execute in two places:
    # - relay (default): the user's Chrome extension dials OUT to /agent;
    # - cloud: a server-side persistent Chromium is attached over CDP.
    extensions = ExtensionRegistry()
    cloud_manager: CloudBrowserManager | None = None
    docker_orchestrator: DockerCloudBrowserOrchestrator | None = None
    browser_mode = os.getenv("TRIPITY_BROWSER_EXECUTOR", "relay").strip().lower()
    browser_ui_authorization: str | None = None
    if browser_mode == "relay":
        browser_executor_factory_for = lambda user: (  # noqa: E731
            lambda origin: RelayExecutor(extensions, user.id, origin)
        )
    elif browser_mode == "cloud":
        cdp_endpoint = os.getenv("TRIPITY_CLOUD_BROWSER_CDP")
        login_url = os.getenv("TRIPITY_CLOUD_BROWSER_LOGIN_URL")
        if not cdp_endpoint:
            raise RuntimeError(
                "TRIPITY_CLOUD_BROWSER_CDP is required when "
                "TRIPITY_BROWSER_EXECUTOR=cloud"
            )
        if not login_url:
            raise RuntimeError(
                "TRIPITY_CLOUD_BROWSER_LOGIN_URL is required when "
                "TRIPITY_BROWSER_EXECUTOR=cloud"
            )
        orchestrator_mode = os.getenv("TRIPITY_CLOUD_BROWSER_ORCHESTRATOR", "template").strip().lower()
        runtime_for_user = None
        if orchestrator_mode == "template":
            pass
        elif orchestrator_mode == "docker":
            browser_ui_password = os.getenv(
                "TRIPITY_CLOUD_BROWSER_PASSWORD", "change-this-before-remote-use"
            )
            docker_orchestrator = DockerCloudBrowserOrchestrator(
                os.getenv("TRIPITY_CLOUD_BROWSER_DOCKER_ROOT", "/data/cloud-browsers"),
                password=browser_ui_password,
            )
            browser_ui_token = base64.b64encode(
                f"tripity:{browser_ui_password}".encode()
            ).decode()
            browser_ui_authorization = f"Basic {browser_ui_token}"

            def runtime_for_user(user):  # type: ignore[no-untyped-def]
                spec = docker_orchestrator.start(user)
                return CloudBrowserRuntime(
                    cdp_endpoint=spec.cdp_endpoint,
                    login_url=spec.login_url,
                    profile_dir=spec.profile_dir,
                )

        else:
            raise RuntimeError(
                "TRIPITY_CLOUD_BROWSER_ORCHESTRATOR must be 'template' or 'docker' "
                f"(got {orchestrator_mode!r})"
            )
        cloud_manager = CloudBrowserManager(
            cdp_endpoint_template=cdp_endpoint,
            login_url_template=login_url,
            profile_root=os.getenv("TRIPITY_CLOUD_BROWSER_PROFILE_ROOT", "/data/browser-profiles"),
            token_ttl_seconds=int(os.getenv("TRIPITY_CLOUD_BROWSER_TOKEN_TTL", "300")),
            runtime_for_user=runtime_for_user,
            on_use_for_user=(
                (lambda user: docker_orchestrator.touch(user))
                if docker_orchestrator is not None
                else None
            ),
        )
        browser_executor_factory_for = cloud_manager.executor_factory_for
    else:
        raise RuntimeError(
            "TRIPITY_BROWSER_EXECUTOR must be 'relay' or 'cloud' "
            f"(got {browser_mode!r})"
        )

    portal = create_portal_app(
        identities,
        connections,
        public_base_url=public_base_url,
        registration_enabled=os.getenv("TRIPITY_REGISTRATION_ENABLED", "true").lower()
        in {"1", "true", "yes"},
        cors_origins=[
            origin.strip()
            for origin in os.getenv("TRIPITY_CORS_ORIGINS", "").split(",")
            if origin.strip()
        ],
        oauth_clients_path=os.getenv(
            "TRIPITY_OAUTH_CLIENTS", "/data/tripity_oauth_clients.json"
        ),
        cloud_browser_login_url_for=(
            (lambda user: cloud_manager.issue_login_url(user, public_base_url))
            if cloud_manager is not None
            else None
        ),
        cloud_browser_status_for=(
            (lambda user: docker_orchestrator.status(user))
            if docker_orchestrator is not None
            else (
                (lambda user: {"profile_dir": str(cloud_manager.profile_dir_for(user)), "profile_exists": cloud_manager.profile_dir_for(user).exists()})
                if cloud_manager is not None
                else None
            )
        ),
        cloud_browser_delete_for=(
            (lambda user: {"deleted": True, **docker_orchestrator.status(user)} if not docker_orchestrator.delete(user) else {"deleted": True})
            if docker_orchestrator is not None
            else None
        ),
        activity_log_path=activity_log_path,
    )
    personal = DynamicPersonalHost(
        identities,
        PersonalGatewayFactory(
            identities,
            connections,
            browser_executor_factory_for=browser_executor_factory_for,
        ),
        public_base_url=public_base_url,
    )

    async def health(_request: object) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def service_root(_request: object) -> JSONResponse:
        return JSONResponse({"service": "tripity", "status": "ok"})

    routes = [
        Route("/health", health),
        Route("/", service_root),
        WebSocketRoute("/agent", extension_websocket_endpoint(identities, extensions)),
    ]
    if cloud_manager is not None:
        async def cloud_browser_session(request: Request) -> Response:
            session = cloud_manager.create_proxy_session(
                request.path_params["token"], identities.users
            )
            if session is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return RedirectResponse(f"/cloud-browser/view/{session.id}/", status_code=302)

        async def cloud_browser_view(request: Request) -> Response:
            session = cloud_manager.proxy_session(request.path_params["session_id"])
            if session is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            path = request.path_params.get("path", "")
            target = urljoin(session.target_base_url + "/", path)
            if request.url.query:
                target += "?" + request.url.query
            headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() not in {"host", "connection", "content-length", "authorization"}
            }
            if browser_ui_authorization is not None:
                headers["authorization"] = browser_ui_authorization
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                    proxied = await client.request(
                        request.method,
                        target,
                        headers=headers,
                        content=await request.body(),
                    )
            except httpx.HTTPError:
                return JSONResponse({"error": "browser ui not ready"}, status_code=502)
            response_headers = {
                key: value
                for key, value in proxied.headers.items()
                if key.lower() not in {
                    "connection",
                    "content-encoding",
                    "content-length",
                    "transfer-encoding",
                }
            }
            return Response(
                proxied.content,
                status_code=proxied.status_code,
                headers=response_headers,
                media_type=proxied.headers.get("content-type"),
            )

        async def cloud_browser_ws(websocket: WebSocket) -> None:
            session = cloud_manager.proxy_session(websocket.path_params["session_id"])
            if session is None:
                await websocket.close(code=4404)
                return
            path = websocket.path_params.get("path", "")
            target_http = urljoin(session.target_base_url + "/", path)
            if websocket.url.query:
                target_http += "?" + websocket.url.query
            split = urlsplit(target_http)
            scheme = "wss" if split.scheme == "https" else "ws"
            target_ws = urlunsplit((scheme, split.netloc, split.path, split.query, split.fragment))
            await websocket.accept()
            headers = {
                key: value
                for key, value in websocket.headers.items()
                if key.lower()
                not in {
                    "host",
                    "connection",
                    "upgrade",
                    "authorization",
                    "sec-websocket-key",
                    "sec-websocket-version",
                    "sec-websocket-extensions",
                }
            }
            if browser_ui_authorization is not None:
                headers["authorization"] = browser_ui_authorization
            try:
                async with websockets.connect(target_ws, additional_headers=headers) as upstream:
                    async def client_to_upstream() -> None:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                await upstream.close()
                                return
                            if "text" in message:
                                await upstream.send(message["text"])
                            elif "bytes" in message:
                                await upstream.send(message["bytes"])

                    async def upstream_to_client() -> None:
                        async for message in upstream:
                            if isinstance(message, bytes):
                                await websocket.send_bytes(message)
                            else:
                                await websocket.send_text(message)

                    tasks = [
                        asyncio.create_task(client_to_upstream()),
                        asyncio.create_task(upstream_to_client()),
                    ]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    for task in done:
                        task.result()
            except WebSocketDisconnect:
                return
            except Exception:
                await websocket.close(code=1011)

        routes.append(Route("/cloud-browser/session/{token}", cloud_browser_session))
        routes.append(
            Route(
                "/cloud-browser/view/{session_id}/{path:path}",
                cloud_browser_view,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        )
        routes.append(
            Route(
                "/cloud-browser/view/{session_id}",
                cloud_browser_view,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        )
        routes.append(WebSocketRoute("/cloud-browser/view/{session_id}/{path:path}", cloud_browser_ws))
        routes.append(WebSocketRoute("/cloud-browser/view/{session_id}", cloud_browser_ws))
    routes.extend([Mount("/mcp", app=personal), Mount("/", app=portal)])

    hibernate_enabled = (
        docker_orchestrator is not None
        and _env_enabled("TRIPITY_CLOUD_BROWSER_HIBERNATE_ENABLED", "false")
    )
    hibernate_interval = float(os.getenv("TRIPITY_CLOUD_BROWSER_HIBERNATE_INTERVAL", "300"))
    hibernate_max_idle = float(os.getenv("TRIPITY_CLOUD_BROWSER_HIBERNATE_MAX_IDLE", "3600"))

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        task: asyncio.Task[None] | None = None
        if hibernate_enabled and docker_orchestrator is not None:
            task = asyncio.create_task(
                _idle_hibernation_scheduler(
                    identities,
                    docker_orchestrator,
                    interval_seconds=hibernate_interval,
                    max_idle_seconds=hibernate_max_idle,
                )
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = Starlette(routes=routes, lifespan=lifespan)
    return ActivityAndRateLimit(
        app,
        log_path=activity_log_path,
        limit=int(os.getenv("TRIPITY_RATE_LIMIT", "120")),
    )


app = create_production_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("TRIPITY_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("TRIPITY_PORT", "8080"))),
        log_level="info",
    )


if __name__ == "__main__":
    main()
