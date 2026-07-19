"""Browser-session connector: run a captured request inside the user's own browser.

Loops 31-32 proved that protected sites (Cloudflare/Turnstile + per-session
tokens, e.g. a CRM the user is logged into) cannot be reached by external
automation — a fresh automated browser is blocked, and only the user's real,
already-authenticated browser session works. This module is the Tripity side of
that model: a captured request (discovered from the user's own browsing, e.g.
via mitmproxy2swagger) becomes an MCP tool whose execution is delegated to a
``SessionExecutor`` that runs it inside the user's live browser session.

The executor is pluggable so the transport (a browser extension, a CDP attach to
the user's real Chrome, or a fake in tests) is independent of the tool wiring.
This drops straight onto a user's personal Tripity gateway alongside the
open-API sidecar connectors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse, urlsplit, urlunsplit

from fastmcp import FastMCP

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _same_origin(origin: str, url: str) -> bool:
    o, u = urlparse(origin), urlparse(url)
    return (o.scheme, o.hostname, o.port) == (u.scheme, u.hostname, u.port)


# Headers the browser manages itself (or that are request-body specific). Replaying
# these from a captured request is wrong or ignored; everything else the app sends
# (nk, x-app-ver, x-requested-with, connect-csrf-token, authorization, ...) is what
# protected sites like Garmin check, so we learn and replay those.
_UNSAFE_HEADERS = frozenset(
    {
        "host",
        "connection",
        "content-length",
        "cookie",
        "accept-encoding",
        "user-agent",
        "referer",
        "origin",
        "content-type",
        "range",
    }
)


# Headers a plain browser fetch of any resource carries anyway. A request whose
# replayable headers are only these is a static asset, not an app API call; the
# app's authenticated calls add tokens/markers on top (nk, x-app-ver, csrf, ...).
_BASELINE_HEADERS = frozenset(
    {"accept", "accept-language", "if-modified-since", "priority", "cache-control", "pragma"}
)


def _header_signal(headers: dict[str, str]) -> int:
    """How many app-specific (non-baseline) headers a request carries."""
    return len(set(headers) - _BASELINE_HEADERS)


# A path that looks like an app API call (vs. a static asset or page route).
_API_MARKERS = ("-service/", "/api/", "/gc-api/", "/rest/", "/v1/", "/v2/", "/proxy/")
_STATIC_SUFFIXES = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".woff", ".woff2",
    ".ttf", ".ico", ".map", ".properties", ".html",
)
# UI-state / consent plumbing, not the user's actual data — kept out of the map
# so the useful data endpoints aren't crowded out by the cap.
_NOISE_MARKERS = (
    "userpreference-service/",
    "gdprconsent-service/",
    "system-service/preference/",
    "/preference/",
)


def _looks_like_api(path: str) -> bool:
    low = path.lower()
    if low.endswith(_STATIC_SUFFIXES):
        return False
    if any(noise in low for noise in _NOISE_MARKERS):
        return False
    return any(marker in low for marker in _API_MARKERS)


def discover_endpoints(urls: list[str], origin: str, *, limit: int = 40) -> list[str]:
    """Pick the API endpoints out of URLs the user's browsing already fetched.

    Keeps same-origin, API-looking paths (dropping the query string so no tokens
    leak and so date/id params don't fragment the list), deduped and sorted. This
    is the map we hand the AI so a cold session can turn "summarise my sleep" into
    the right path without the user knowing any endpoint.
    """
    paths: set[str] = set()
    for url in urls:
        if not _same_origin(origin, url):
            continue
        path = urlsplit(url).path
        if _looks_like_api(path):
            paths.add(path)
    return sorted(paths)[:limit]


def _replayable_headers(raw: dict[str, str]) -> dict[str, str]:
    """Keep the app's own request headers, drop the browser-managed ones.

    The site's SPA adds custom headers (and per-session tokens) to its XHR/fetch
    calls; a bare fetch omits them and protected gateways answer 403. We mirror
    exactly those custom headers, lower-cased, minus anything the browser sets by
    itself or that pins a request body.
    """
    out: dict[str, str] = {}
    for key, value in raw.items():
        low = key.lower()
        if low in _UNSAFE_HEADERS or low.startswith((":", "sec-")):
            continue
        out[low] = value
    return out


class SessionExecutor(Protocol):
    """Runs an HTTP request inside the user's authenticated browser session."""

    async def execute(
        self, method: str, url: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class BrowserRequest:
    """A read-only request captured from the user's own browsing.

    ``url_template`` may contain ``{name}`` placeholders that are filled from the
    tool's arguments; any remaining arguments are appended as query parameters.
    """

    name: str
    url_template: str
    method: str = "GET"
    description: str = ""
    params: tuple[str, ...] = field(default_factory=tuple)

    def template_params(self) -> tuple[str, ...]:
        return tuple(_PLACEHOLDER.findall(self.url_template))

    def resolve(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return (final_url, leftover_query_params) for the given arguments."""
        used: set[str] = set()

        def _sub(match: re.Match[str]) -> str:
            key = match.group(1)
            used.add(key)
            if key not in arguments:
                raise KeyError(f"Missing required parameter: {key}")
            return str(arguments[key])

        url = _PLACEHOLDER.sub(_sub, self.url_template)
        query = {
            k: v for k, v in arguments.items() if k not in used and v is not None
        }
        return url, query


def register_browser_tool(
    gateway: FastMCP,
    request: BrowserRequest,
    executor: SessionExecutor,
) -> None:
    """Expose one captured request as an MCP tool backed by the browser session."""

    param_names = list(
        dict.fromkeys([*request.template_params(), *request.params])
    )

    async def _run(**arguments: Any) -> dict[str, Any]:
        url, query = request.resolve(arguments)
        return await executor.execute(request.method, url, params=query)

    _run.__name__ = request.name
    # Give the tool an explicit signature AND matching annotations so MCP clients
    # (which build the schema via typing.get_type_hints) see the real parameters.
    if param_names:
        import inspect

        _run.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [
                inspect.Parameter(
                    name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Any
                )
                for name in param_names
            ]
        )
        _run.__annotations__ = {name: Any for name in param_names}
        _run.__annotations__["return"] = dict
    gateway.tool(
        _run,
        name=request.name,
        description=request.description or f"{request.method} {request.url_template}",
    )


def register_origin_query(
    gateway: FastMCP,
    origin: str,
    executor: SessionExecutor,
    *,
    tool_name: str = "query",
    endpoints: list[str] | None = None,
) -> None:
    """Expose ONE generic tool giving the AI full access to a connected origin.

    Instead of only the endpoints we happened to record, the AI composes any
    request (path + query params) against the connected site, run inside the
    user's authenticated session. Access is bounded to ``origin`` so the AI
    cannot pivot to another site. This is what makes a "connection" mean the
    user's full access to that system, not a single captured call.
    """
    origin = origin.rstrip("/")

    async def query(path: str, method: str = "GET", params: dict | None = None) -> dict[str, Any]:
        split = urlsplit(path if "://" in path else origin + "/" + path.lstrip("/"))
        query_string = urlencode(params) if params else split.query
        url = urlunsplit(
            (split.scheme, split.netloc, split.path, query_string, split.fragment)
        )
        if not _same_origin(origin, url):
            raise ValueError(
                f"Refusing to query outside the connected origin {origin}: {url}"
            )
        return await executor.execute(method.upper(), url, params={})

    doc = (
        f"Call any endpoint of {origin} in the user's authenticated browser "
        "session. path is relative to the site root; params is a dict of query "
        "parameters. Returns the JSON (or text) response."
    )
    if endpoints:
        listed = "\n".join(f"  {path}" for path in endpoints)
        doc += (
            "\n\nEndpoints discovered from the user's own browsing (pick the one "
            "that fits the request; add query params as needed):\n" + listed
        )
    query.__doc__ = doc
    gateway.tool(query, name=tool_name)


def build_browser_connector(
    name: str,
    requests: list[BrowserRequest],
    executor: SessionExecutor,
    *,
    origin: str | None = None,
) -> FastMCP:
    """Build a standalone MCP server exposing a connected site via the browser.

    When ``origin`` is given, also registers the generic ``query`` tool that
    grants full access to that origin (not just the captured ``requests``).
    """
    gateway: FastMCP = FastMCP(name=name)
    for request in requests:
        register_browser_tool(gateway, request, executor)
    if origin:
        register_origin_query(gateway, origin, executor)
    return gateway


class CdpSessionExecutor:
    """A SessionExecutor that runs requests inside the user's real Chrome.

    Attaches to a Chrome already running with remote debugging
    (``chrome --remote-debugging-port=9222``) and executes each request via the
    page's own ``fetch`` with ``credentials: 'include'`` — so the user's cookies,
    login and any Cloudflare clearance apply automatically. This is the bridge
    that a browser-resident Tripity would use; fresh automated browsers are
    blocked by anti-bot checks (see LOOP_33_RESULTS.md), the user's real session
    is not.
    """

    def __init__(
        self,
        cdp_endpoint: str = "http://127.0.0.1:9222",
        *,
        origin: str | None = None,
    ) -> None:
        self.cdp_endpoint = cdp_endpoint
        self.origin = origin
        # Headers learned from the site's own requests, reused across calls in
        # this process so we only pay the learning cost once per connection.
        self._learned_headers: dict[str, str] = {}

    async def _pick_page(self, browser: Any) -> Any:
        pages = [pg for ctx in browser.contexts for pg in ctx.pages]
        if not pages:
            raise RuntimeError("No open pages in the attached browser")
        if self.origin:
            for pg in pages:
                if _same_origin(self.origin, pg.url):
                    return pg
            # Fall back to the first page and navigate it to the origin.
            await pages[0].goto(self.origin, wait_until="domcontentloaded")
            return pages[0]
        return pages[0]

    async def _learn_headers(self, page: Any, timeout_ms: int = 10000) -> dict[str, str]:
        """Capture the app's own custom headers from one live same-origin call.

        Protected gateways (e.g. Garmin's ``/gc-api/``) reject a bare fetch that
        lacks the SPA's headers and per-session token. We mirror those headers by
        observing one of the app's real XHR/fetch requests:

        1. Passively — in case the page is already making requests.
        2. Actively — if the page is idle, open a short-lived background tab to
           the same origin (same session → real per-session token) to make the
           app emit its authenticated calls, capture one, and close the tab.

        Best-effort: if nothing is captured we fall back to a bare fetch (which
        still works for cookie-only sites). Learned headers are cached.
        """
        origin = self.origin or page.url
        candidates: list[Any] = []

        def _collect(request: Any) -> None:
            if request.resource_type in ("xhr", "fetch") and _same_origin(
                origin, request.url
            ):
                candidates.append(request)

        # A throwaway tab in the SAME context (same cookies/session) reloads the
        # origin, forcing the app's authenticated calls; we watch both it and the
        # user's page, then keep the request carrying the most app-specific
        # headers (the API call), not a static asset that fetched first.
        page.on("request", _collect)
        learner = None
        try:
            learner = await page.context.new_page()
            learner.on("request", _collect)
            target = page.url if _same_origin(origin, page.url) else origin
            await learner.goto(target, wait_until="domcontentloaded")
            await learner.wait_for_timeout(min(timeout_ms, 4000))
        except Exception:  # noqa: BLE001 - learning is best-effort, never fatal
            pass
        finally:
            page.remove_listener("request", _collect)
            best: dict[str, str] = {}
            for request in candidates:
                try:
                    headers = _replayable_headers(await request.all_headers())
                except Exception:  # noqa: BLE001
                    continue
                if _header_signal(headers) > _header_signal(best):
                    best = headers
            if best:
                self._learned_headers = best
            if learner is not None:
                await learner.close()
        return self._learned_headers

    async def discover(self) -> list[str]:
        """Collect the API endpoints the user's own browsing already hit.

        Reads every open same-origin tab's performance resource entries (which
        include XHR/fetch calls), so the endpoints the user naturally visited
        become the map we show the AI. No navigation, no side effects.
        """
        from playwright.async_api import async_playwright

        origin = self.origin
        urls: list[str] = []
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(self.cdp_endpoint)
            try:
                for ctx in browser.contexts:
                    for page in ctx.pages:
                        if origin and not _same_origin(origin, page.url):
                            continue
                        try:
                            found = await page.evaluate(
                                "() => performance.getEntriesByType('resource').map(e => e.name)"
                            )
                            urls.extend(found)
                        except Exception:  # noqa: BLE001 - skip a page we can't read
                            continue
            finally:
                await browser.close()
        return discover_endpoints(urls, origin or "")

    async def execute(
        self, method: str, url: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        if params:
            split = urlsplit(url)
            merged = urlencode(params) if not split.query else split.query + "&" + urlencode(params)
            url = urlunsplit(
                (split.scheme, split.netloc, split.path, merged, split.fragment)
            )

        fetch_js = """async ([u, m, h]) => {
            const r = await fetch(u, { method: m, headers: h, credentials: 'include' });
            const text = await r.text();
            try { return { status: r.status, data: JSON.parse(text) }; }
            catch (e) { return { status: r.status, text: text.slice(0, 20000) }; }
        }"""

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(self.cdp_endpoint)
            try:
                page = await self._pick_page(browser)
                if not self._learned_headers:
                    await self._learn_headers(page)
                result = await page.evaluate(
                    fetch_js, [url, method, self._learned_headers]
                )
                # A protected gateway may still 401/403 if we had no headers yet
                # or the session token rotated: re-learn once and retry.
                if result.get("status") in (401, 403):
                    self._learned_headers = {}
                    await self._learn_headers(page)
                    if self._learned_headers:
                        result = await page.evaluate(
                            fetch_js, [url, method, self._learned_headers]
                        )
                return result
            finally:
                await browser.close()
