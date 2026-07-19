"""Outbound relay: run browser fetches inside the user's own session via a Tripity
extension that dials OUT to the hub.

Loops 34-39 proved the browser-session model with ``CdpSessionExecutor``, which
attaches to a Chrome running locally on the same machine as the MCP process
(``127.0.0.1:9222``). That ties the user to a local process and can't work once
the hub is in the cloud (loop 43): the cloud can't reach the user's Chrome behind
NAT.

This module removes that dependency. A Tripity browser extension opens an
*outbound* WebSocket to the hub (browser dials out — no NAT problem, no local
server, no debugging port, no external service). When the AI calls a browser
connection's tool, the hub routes the request DOWN that socket to the extension,
which runs ``fetch(..., {credentials: 'include'})`` in the user's authenticated
session and returns the result UP. ``RelayExecutor`` is a drop-in
``SessionExecutor`` — the connection store, endpoint discovery, personal gateway
and portal are unchanged.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from tripity_experiment.browser_connector import discover_endpoints

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

    from tripity_experiment.identity import IdentityStore

Sender = Callable[[dict[str, Any]], Awaitable[None]]


class ExtensionNotConnected(RuntimeError):
    """Raised when a user has no live extension to run a browser request."""


class ExtensionChannel:
    """One connected browser extension for one user.

    Request/response is multiplexed over a duplex message transport (a WebSocket
    in production, a fake in tests) by tagging each request with an id and
    resolving the matching future when its reply arrives.
    """

    def __init__(self, send: Sender) -> None:
        self._send = send
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def request(
        self, payload: dict[str, Any], *, timeout: float = 30.0
    ) -> dict[str, Any]:
        request_id = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send({**payload, "id": request_id})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, message: dict[str, Any]) -> None:
        """Deliver a reply from the extension to whoever is awaiting its id."""
        future = self._pending.get(str(message.get("id", "")))
        if future is not None and not future.done():
            future.set_result(message)

    def fail_all(self, exc: Exception) -> None:
        """Fail every in-flight request (e.g. the extension disconnected)."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


class ExtensionRegistry:
    """Which users currently have a live extension connected.

    Last writer wins: if a user opens the extension in a second browser the newer
    socket takes over. A channel only unregisters itself, so a stale disconnect
    can't evict a fresher connection.
    """

    def __init__(self) -> None:
        self._channels: dict[str, ExtensionChannel] = {}

    def register(self, user_id: str, channel: ExtensionChannel) -> None:
        self._channels[user_id] = channel

    def unregister(self, user_id: str, channel: ExtensionChannel) -> None:
        if self._channels.get(user_id) is channel:
            del self._channels[user_id]

    def channel(self, user_id: str) -> ExtensionChannel | None:
        return self._channels.get(user_id)


class RelayExecutor:
    """A ``SessionExecutor`` that runs each request in the user's browser via
    their connected extension, instead of attaching to a local Chrome."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        user_id: str,
        origin: str | None = None,
    ) -> None:
        self.registry = registry
        self.user_id = user_id
        self.origin = origin

    def _channel(self) -> ExtensionChannel:
        channel = self.registry.channel(self.user_id)
        if channel is None:
            raise ExtensionNotConnected(
                "Your Tripity browser is not connected. Open the browser where you "
                "installed the Tripity extension (and are logged into the site), "
                "then try again."
            )
        return channel

    async def execute(
        self, method: str, url: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        message = await self._channel().request(
            {"op": "fetch", "method": method, "url": url, "params": params}
        )
        if message.get("error"):
            return {"status": 0, "error": str(message["error"])}
        result = message.get("result")
        if not isinstance(result, dict):
            return {"status": 0, "error": "empty relay response"}
        return result

    async def discover(self) -> list[str]:
        message = await self._channel().request(
            {"op": "discover", "origin": self.origin}
        )
        urls = message.get("urls") or []
        return discover_endpoints(list(urls), self.origin or "")


def extension_websocket_endpoint(
    identities: "IdentityStore",
    registry: ExtensionRegistry,
    *,
    ping_interval: float = 20.0,
) -> Callable[["WebSocket"], Awaitable[None]]:
    """Starlette endpoint the Tripity extension dials out to.

    The extension sends ``{"token": <access_token>}`` first; we authenticate it,
    register its channel under the owner's id, then pump replies back to whoever
    is awaiting them. Tokens are never logged. Auth is per-user, so one user's
    channel can never be routed to another user's request.

    We push a ``{"type":"ping"}`` every ``ping_interval`` seconds: receiving a
    message keeps the extension's MV3 service worker alive (Chrome kills an idle
    worker after ~30s, which would drop the socket), so the connection survives
    when there's no relay traffic.
    """

    from starlette.websockets import WebSocketDisconnect

    async def endpoint(websocket: "WebSocket") -> None:
        await websocket.accept()
        try:
            hello = await websocket.receive_json()
        except Exception:  # noqa: BLE001 - malformed handshake, just drop it
            await websocket.close(code=4400)
            return

        token = hello.get("token", "") if isinstance(hello, dict) else ""
        user = identities.authenticate(token) if token else None
        if user is None:
            await websocket.send_json({"type": "error", "error": "unauthorized"})
            await websocket.close(code=4401)
            return

        async def send(payload: dict[str, Any]) -> None:
            await websocket.send_json(payload)

        channel = ExtensionChannel(send)
        registry.register(user.id, channel)
        await websocket.send_json({"type": "ready"})

        async def keepalive() -> None:
            try:
                while True:
                    await asyncio.sleep(ping_interval)
                    await websocket.send_json({"type": "ping"})
            except Exception:  # noqa: BLE001 - socket closing ends the pinger
                pass

        pinger = asyncio.create_task(keepalive())
        try:
            while True:
                channel.resolve(await websocket.receive_json())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            pinger.cancel()
            channel.fail_all(ExtensionNotConnected("extension disconnected"))
            registry.unregister(user.id, channel)

    return endpoint
