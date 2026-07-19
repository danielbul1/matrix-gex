"""Server-side browser execution for the cloud-browser architecture.

Loop 62A proved a self-hosted Chromium can hold the user's Garmin session and
serve authenticated JSON when Tripity attaches over CDP. This module is the seam
that lets production choose where browser connections run:

- extension relay (current default): requests go to the user's open Chrome
  extension over WebSocket;
- cloud CDP: requests run in a server-side persistent Chromium profile.

Loop 64 adds the first per-user management layer: endpoint/profile/login URL
*templates* plus one-time login tokens. A later orchestrator can replace the
same interface with real container scheduling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import secrets
import time
from typing import Any

from starlette.responses import JSONResponse, RedirectResponse

from tripity_experiment.browser_connector import CdpSessionExecutor, SessionExecutor
from tripity_experiment.gateway import BrowserExecutorFactory
from tripity_experiment.identity import User


@dataclass(frozen=True)
class CloudBrowserRuntime:
    """Resolved runtime details for one user's cloud browser."""

    cdp_endpoint: str
    login_url: str
    profile_dir: Path


@dataclass(frozen=True)
class CloudBrowserProxySession:
    """Short-lived browser UI proxy session created from a one-time token."""

    id: str
    user_id: str
    target_base_url: str
    expires_at: float


@dataclass(frozen=True)
class CloudBrowserConfig:
    """Configuration for server-side CDP browser execution.

    Template fields supported by endpoint/login/profile settings:
    ``{user_id}``, ``{public_id}``, and ``{username}``.
    """

    cdp_endpoint: str

    def endpoint_for(self, user: User) -> str:
        return _format_user(self.cdp_endpoint, user)


def _format_user(template: str, user: User) -> str:
    return template.format(
        user_id=user.id,
        public_id=user.public_id,
        username=user.username,
    )


def cloud_browser_executor_factory_for(
    config: CloudBrowserConfig,
) -> Callable[[User], BrowserExecutorFactory]:
    """Return the PersonalGatewayFactory hook for cloud-browser execution."""

    def for_user(user: User) -> BrowserExecutorFactory:
        endpoint = config.endpoint_for(user)

        def for_origin(origin: str) -> SessionExecutor:
            return CdpSessionExecutor(endpoint, origin=origin)

        return for_origin

    return for_user


class TouchingSessionExecutor:
    """SessionExecutor wrapper that marks the user's browser active on use."""

    def __init__(
        self,
        wrapped: SessionExecutor,
        on_use: Callable[[], None] | None = None,
    ) -> None:
        self.wrapped = wrapped
        self.on_use = on_use

    def _touch(self) -> None:
        if self.on_use is not None:
            self.on_use()

    async def execute(self, method: str, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
        self._touch()
        return await self.wrapped.execute(method, url, params=params)

    async def discover(self) -> list[str]:
        self._touch()
        discover = getattr(self.wrapped, "discover")
        return await discover()


class CloudBrowserManager:
    """Per-user cloud-browser manager for the MVP architecture.

    This does not yet start/stop containers. It owns the stable contract that a
    scheduler will later satisfy: for a user, produce a CDP endpoint, a profile
    directory, and a short-lived one-time URL that lets the user log into the
    remote browser view without exposing CDP.
    """

    def __init__(
        self,
        *,
        cdp_endpoint_template: str,
        login_url_template: str,
        profile_root: str | Path,
        token_ttl_seconds: int = 300,
        runtime_for_user: Callable[[User], CloudBrowserRuntime] | None = None,
        on_use_for_user: Callable[[User], None] | None = None,
    ) -> None:
        self.cdp_endpoint_template = cdp_endpoint_template
        self.login_url_template = login_url_template
        self.profile_root = Path(profile_root)
        self.token_ttl_seconds = token_ttl_seconds
        self.runtime_for_user = runtime_for_user
        self.on_use_for_user = on_use_for_user
        self._tokens: dict[str, tuple[str, float]] = {}
        self._proxy_sessions: dict[str, CloudBrowserProxySession] = {}

    def runtime_for(self, user: User) -> CloudBrowserRuntime:
        if self.runtime_for_user is not None:
            return self.runtime_for_user(user)
        return CloudBrowserRuntime(
            cdp_endpoint=_format_user(self.cdp_endpoint_template, user),
            login_url=_format_user(self.login_url_template, user),
            profile_dir=self.profile_root / user.public_id,
        )

    def cdp_endpoint_for(self, user: User) -> str:
        return self.runtime_for(user).cdp_endpoint

    def login_target_for(self, user: User) -> str:
        return self.runtime_for(user).login_url

    def profile_dir_for(self, user: User) -> Path:
        return self.runtime_for(user).profile_dir

    def ensure_profile(self, user: User) -> Path:
        path = self.profile_dir_for(user)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def executor_factory_for(self, user: User) -> BrowserExecutorFactory:
        self.ensure_profile(user)
        endpoint = self.cdp_endpoint_for(user)

        def for_origin(origin: str) -> SessionExecutor:
            on_use = (lambda: self.on_use_for_user(user)) if self.on_use_for_user else None
            return TouchingSessionExecutor(
                CdpSessionExecutor(endpoint, origin=origin),
                on_use=on_use,
            )

        return for_origin

    def issue_login_url(self, user: User, public_base_url: str) -> str:
        self.ensure_profile(user)
        token = secrets.token_urlsafe(24)
        self._tokens[token] = (user.id, time.time() + self.token_ttl_seconds)
        return f"{public_base_url.rstrip('/')}/cloud-browser/session/{token}"

    def consume_login_token(self, token: str, *, now: float | None = None) -> str | None:
        record = self._tokens.pop(token, None)
        if record is None:
            return None
        _user_id, expires_at = record
        if expires_at < (time.time() if now is None else now):
            return None
        return _user_id

    def consume_login_target(
        self,
        token: str,
        users: Callable[[], list[User]],
        *,
        now: float | None = None,
    ) -> str | None:
        session = self.create_proxy_session(token, users, now=now)
        return session.target_base_url if session else None

    def create_proxy_session(
        self,
        token: str,
        users: Callable[[], list[User]],
        *,
        now: float | None = None,
    ) -> CloudBrowserProxySession | None:
        now = time.time() if now is None else now
        user_id = self.consume_login_token(token, now=now)
        if user_id is None:
            return None
        user = next((candidate for candidate in users() if candidate.id == user_id), None)
        if user is None:
            return None
        session = CloudBrowserProxySession(
            id=secrets.token_urlsafe(18),
            user_id=user.id,
            target_base_url=self.login_target_for(user).rstrip("/"),
            expires_at=now + self.token_ttl_seconds,
        )
        self._proxy_sessions[session.id] = session
        return session

    def proxy_session(self, session_id: str, *, now: float | None = None) -> CloudBrowserProxySession | None:
        session = self._proxy_sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at < (time.time() if now is None else now):
            self._proxy_sessions.pop(session_id, None)
            return None
        return session


async def cloud_browser_login_route(
    scope: dict[str, Any],
    receive: Any,
    send: Any,
    *,
    manager: CloudBrowserManager,
    users: Callable[[], list[User]],
) -> None:
    """ASGI route for `/cloud-browser/session/{token}`.

    Starlette's Route passes the token in `scope['path_params']`. A valid token
    is one-time and redirects to the configured browser UI. Invalid/expired
    tokens return 404 so URLs are not reusable.
    """

    token = scope.get("path_params", {}).get("token", "")
    target = manager.consume_login_target(token, users)
    response = (
        RedirectResponse(target, status_code=302)
        if target
        else JSONResponse({"error": "not found"}, status_code=404)
    )
    await response(scope, receive, send)
