"""Authenticated entry point for a user's private MCP gateway."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp import FastMCP

from tripity_experiment.connection_store import ConnectionStore
from tripity_experiment.gateway import (
    BrowserExecutorFactory,
    SpecFetcher,
    build_gateway,
    restore_connections,
)
from tripity_experiment.identity import IdentityStore, User
from tripity_experiment.openapi_intake import fetch_openapi_url
from tripity_experiment.project_gateway import BearerGate


@dataclass(frozen=True)
class PersonalGateway:
    user: User
    gateway: FastMCP
    app: Any
    restored: tuple[str, ...]


class PersonalGatewayFactory:
    """Resolve an access token to exactly one owner-scoped MCP server."""

    def __init__(
        self,
        identities: IdentityStore,
        connections: ConnectionStore,
        *,
        spec_fetcher: SpecFetcher = fetch_openapi_url,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
        browser_executor_factory_for: (
            Callable[[User], BrowserExecutorFactory] | None
        ) = None,
    ) -> None:
        self.identities = identities
        self.connections = connections
        self.spec_fetcher = spec_fetcher
        self.upstream_transport = upstream_transport
        # Builds the per-user SessionExecutor factory for browser connections.
        # Default (None) keeps the local CDP executor so dev and tests are
        # unchanged; production injects one that relays to the user's extension.
        self.browser_executor_factory_for = browser_executor_factory_for

    async def create(self, access_token: str) -> PersonalGateway:
        user = self.identities.authenticate(access_token)
        if user is None:
            raise PermissionError("Invalid access token")
        return await self.create_for_user(user, access_token)

    async def create_for_user(
        self,
        user: User,
        access_token: str | None = None,
    ) -> PersonalGateway:
        browser: dict[str, BrowserExecutorFactory] = {}
        if self.browser_executor_factory_for is not None:
            browser["browser_executor_factory"] = self.browser_executor_factory_for(
                user
            )
        gateway = build_gateway(
            name=f"Tripity — {user.username}",
            store=self.connections,
            owner_id=user.id,
            spec_fetcher=self.spec_fetcher,
            upstream_transport=self.upstream_transport,
            **browser,
        )
        restored = await restore_connections(
            gateway,
            self.connections,
            owner_id=user.id,
            spec_fetcher=self.spec_fetcher,
            upstream_transport=self.upstream_transport,
            **browser,
        )
        raw_app = gateway.http_app(path="/mcp", stateless_http=False)
        app = BearerGate(raw_app, access_token) if access_token else raw_app
        return PersonalGateway(user, gateway, app, tuple(restored))
