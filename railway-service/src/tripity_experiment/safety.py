"""Small pilot safety envelope: redacted activity log and fixed-window limits."""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import time
from typing import Any


class ActivityAndRateLimit:
    def __init__(
        self,
        app: Any,
        *,
        log_path: str | Path,
        limit: int = 120,
        window_seconds: int = 60,
    ) -> None:
        self.app = app
        self.log_path = Path(log_path)
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _actor(
        self,
        headers: dict[bytes, bytes],
        client: tuple[str, int] | None = None,
    ) -> str:
        authorization = headers.get(b"authorization", b"")
        if authorization:
            identity = b"token:" + authorization
        else:
            # Railway appends the connecting address to X-Forwarded-For. Use
            # the final value so a caller cannot evade limits by prepending a
            # spoofed address.
            forwarded = headers.get(b"x-forwarded-for", b"")
            address = forwarded.split(b",")[-1].strip() if forwarded else b""
            if not address and client:
                address = client[0].encode()
            identity = b"ip:" + (address or b"unknown")
        return hashlib.sha256(identity).hexdigest()[:12]

    def _write(self, record: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        actor = self._actor(headers, scope.get("client"))
        now = time.monotonic()
        hits = self._hits[actor]
        while hits and hits[0] <= now - self.window_seconds:
            hits.popleft()
        if len(hits) >= self.limit:
            self._write(
                {"event": "rate_limited", "actor": actor, "path": scope.get("path", ""), "ts": time.time()}
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {"type": "http.response.body", "body": b'{"error":"rate_limit_exceeded"}'}
            )
            return
        hits.append(now)
        started = time.monotonic()
        status = 500

        async def capture(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        await self.app(scope, receive, capture)
        self._write(
            {
                "event": "request",
                "actor": actor,
                "ts": time.time(),
                "method": scope.get("method", ""),
                "path": scope.get("path", ""),
                "status": status,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
