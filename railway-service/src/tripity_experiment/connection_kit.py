"""Turn picked operations into a ready-to-run sidecar + connection steps.

This unifies the no-code UI (loops 3-5) with the real deployable sidecar
(loops 7-16): instead of an ephemeral localhost server, the user gets the exact
sidecar configuration for the operations they picked, plus plain-language steps
to connect their AI — no operationId, no JSON, no hand-written env.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal

AuthMode = Literal["oauth", "bearer", "none"]

DEFAULT_IMAGE = "tripity-sidecar"


def build_connection_kit(
    *,
    source_type: Literal["url", "text"],
    source: str,
    api_base_url: str,
    selected_operation_ids: list[str],
    auth: AuthMode = "oauth",
    image: str = DEFAULT_IMAGE,
    public_url: str | None = None,
) -> dict[str, Any]:
    """Build the sidecar env, a runnable docker command, and connect steps."""
    if not selected_operation_ids:
        raise ValueError("Pick at least one operation")

    env: dict[str, str] = {}
    if source_type == "url":
        env["TRIPITY_OPENAPI_URL"] = source
    else:
        # A pasted spec is mounted into the container as a file.
        env["TRIPITY_OPENAPI_FILE"] = "/openapi.json"
    env["TRIPITY_API_BASE_URL"] = api_base_url
    env["TRIPITY_ALLOWED_OPERATIONS"] = ",".join(selected_operation_ids)

    generated_token: str | None = None
    if auth == "oauth":
        env["TRIPITY_OAUTH_ENABLED"] = "1"
        env["TRIPITY_PUBLIC_URL"] = public_url or "https://YOUR-PUBLIC-URL"
    elif auth == "bearer":
        generated_token = secrets.token_urlsafe(24)
        env["TRIPITY_CLIENT_BEARER_TOKEN"] = generated_token
    # auth == "none": no auth env; endpoint is open (only for trusted networks)

    parts = ["docker run -p 8000:8000"]
    if source_type == "text":
        parts.append("-v ./openapi.json:/openapi.json:ro")
    for key, value in env.items():
        parts.append(f'-e {key}="{value}"')
    parts.append(image)
    docker_run = " \\\n  ".join(parts)

    mcp_url = f"{(public_url or 'https://YOUR-PUBLIC-URL').rstrip('/')}/mcp"
    connect_steps = {
        "chatgpt": [
            "הרץ את הפקודה למעלה — היא מפעילה את החיבור על השרת שלך.",
            "ודא שהכתובת נגישה ב-HTTPS (ליד ה-API הציבורי שלך, או דרך ה-ingress שלך).",
            "ChatGPT → Settings → Apps → Advanced settings → Create app.",
            f"Connection: Server URL → הדבק {mcp_url}",
            "Authentication: "
            + ("OAuth (מומלץ)" if auth == "oauth" else "No Auth" if auth == "none" else "טוקן"),
            "לחץ Create, ואז בצ'אט בקש להשתמש בכלי.",
        ],
        "claude": [
            "הרץ את הפקודה למעלה.",
            "ודא שהכתובת נגישה ב-HTTPS.",
            "Claude.ai → Settings → Connectors → Add custom connector.",
            f"הדבק {mcp_url} ובחר אימות בהתאם.",
        ],
    }

    return {
        "env": env,
        "docker_run": docker_run,
        "mcp_url": mcp_url,
        "auth": auth,
        "allowed_operations": list(selected_operation_ids),
        "client_bearer_token": generated_token,
        "connect_steps": connect_steps,
    }
