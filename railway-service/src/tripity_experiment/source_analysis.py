from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


@dataclass(frozen=True)
class SourceAnalysis:
    kind: str
    url: str
    message: str
    mcp_url: str | None = None
    openapi_url: str | None = None
    auth: str | None = None
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "message": self.message,
            "mcp_url": self.mcp_url,
            "openapi_url": self.openapi_url,
            "auth": self.auth,
            "evidence": list(self.evidence),
        }


def _origin(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def candidate_mcp_urls(input_url: str) -> tuple[str, ...]:
    parsed = urlsplit(input_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ()
    candidates = []
    if "/mcp" in parsed.path or parsed.path.rstrip("/").endswith("mcp"):
        candidates.append(input_url.rstrip("/"))
    origin = _origin(input_url)
    if origin:
        candidates.extend([urljoin(origin, "/mcp"), urljoin(origin, "/api/mcp")])
    return tuple(dict.fromkeys(candidates))


def _protected_resource_url(mcp_url: str) -> str | None:
    parsed = urlsplit(mcp_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{parsed.path}"


def analyze_source_url(url: str, *, timeout_seconds: float = 8, transport: httpx.BaseTransport | None = None) -> SourceAnalysis:
    url = url.strip()
    if not url:
        return SourceAnalysis(kind="invalid", url=url, message="URL is required")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return SourceAnalysis(kind="invalid", url=url, message="Use a full http(s) URL")

    evidence: list[str] = []
    with httpx.Client(timeout=timeout_seconds, follow_redirects=False, transport=transport) as client:
        for candidate in candidate_mcp_urls(url):
            metadata_url = _protected_resource_url(candidate)
            if metadata_url:
                try:
                    response = client.get(metadata_url, headers={"accept": "application/json"})
                    if response.status_code == 200:
                        data = response.json()
                        auth = "oauth" if data.get("authorization_servers") else "unknown"
                        return SourceAnalysis(
                            kind="existing_mcp",
                            url=url,
                            mcp_url=candidate,
                            auth=auth,
                            message="This already looks like an MCP server. Use this MCP URL directly, or wrap/curate it later.",
                            evidence=(f"OAuth protected resource metadata: {metadata_url}",),
                        )
                    evidence.append(f"{metadata_url} -> {response.status_code}")
                except Exception as exc:  # noqa: BLE001
                    evidence.append(f"{metadata_url} -> {type(exc).__name__}")
            try:
                response = client.get(candidate)
                allow = response.headers.get("allow", "")
                if response.status_code in {401, 405} and "POST" in allow.upper():
                    return SourceAnalysis(
                        kind="existing_mcp",
                        url=url,
                        mcp_url=candidate,
                        auth="unknown",
                        message="This endpoint behaves like an MCP HTTP endpoint.",
                        evidence=(f"GET {candidate} -> {response.status_code}; allow={allow}",),
                    )
                evidence.append(f"GET {candidate} -> {response.status_code}")
            except Exception as exc:  # noqa: BLE001
                evidence.append(f"GET {candidate} -> {type(exc).__name__}")

    return SourceAnalysis(
        kind="needs_assisted_setup",
        url=url,
        message="No existing MCP endpoint was detected. Tripity will try OpenAPI discovery or assisted setup next.",
        evidence=tuple(evidence),
    )
