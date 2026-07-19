from __future__ import annotations

from dataclasses import dataclass
import secrets
import socket
import threading
import time
from typing import Any

import httpx
import uvicorn

from tripity_experiment.mcp_adapter import build_mcp_server


class BearerGate:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            supplied = headers.get(b"authorization", b"")
            expected = f"Bearer {self.token}".encode()
            if not secrets.compare_digest(supplied, expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error":"unauthorized"}',
                    }
                )
                return
        await self.app(scope, receive, send)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class TemporaryProject:
    project_id: str
    access_token: str
    mcp_url: str
    server: uvicorn.Server
    thread: threading.Thread
    operation_ids: tuple[str, ...]

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class TemporaryProjectManager:
    def __init__(self) -> None:
        self.projects: dict[str, TemporaryProject] = {}

    def create(
        self,
        *,
        openapi_spec: dict[str, Any],
        base_url: str,
        operation_ids: list[str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> TemporaryProject:
        project_id = secrets.token_urlsafe(9)
        token = secrets.token_urlsafe(32)
        port = _free_port()
        mcp = build_mcp_server(
            openapi_spec=openapi_spec,
            base_url=base_url,
            allowed_operation_ids=operation_ids,
            transport=transport,
        )
        protected_app = BearerGate(
            mcp.http_app(path="/mcp", stateless_http=True),
            token,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                protected_app,
                host="127.0.0.1",
                port=port,
                log_level="error",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=2)
            raise RuntimeError("Temporary MCP server did not start")
        project = TemporaryProject(
            project_id=project_id,
            access_token=token,
            mcp_url=f"http://127.0.0.1:{port}/mcp",
            server=server,
            thread=thread,
            operation_ids=tuple(operation_ids),
        )
        self.projects[project_id] = project
        return project

    def close(self) -> None:
        for project in list(self.projects.values()):
            project.stop()
        self.projects.clear()

