"""Tripity connection registry — the central *connection* plane.

This is the first brick of the "neutral like Amazon" model: Tripity is central
to the CONNECTION (register + discover) but never touches the DATA. A customer
registers where their self-hosted sidecar lives; an AI-side client discovers it
and then connects **directly** to that sidecar.

Deliberately, this service has no endpoint that proxies MCP tool traffic. Tool
calls and API data cannot structurally pass through Tripity — they go
peer-to-peer to the customer's sidecar. Identity/trust is a later brick; this
one only proves the connection/data split.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator


class ConnectionInput(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    sidecar_mcp_url: str
    tools: list[str] = Field(default_factory=list)

    @field_validator("sidecar_mcp_url")
    @classmethod
    def _url_must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("sidecar_mcp_url must be an http(s) URL")
        return value


class Connection(ConnectionInput):
    connection_id: str
    created_at: str


def create_registry_app() -> FastAPI:
    app = FastAPI(
        title="Tripity Connection Registry",
        version="0.1.0",
        description="Central connection discovery. Never proxies tool data.",
    )
    connections: dict[str, Connection] = {}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/connections", status_code=201)
    def register(body: ConnectionInput) -> Connection:
        connection_id = secrets.token_hex(8)
        connection = Connection(
            connection_id=connection_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            **body.model_dump(),
        )
        connections[connection_id] = connection
        return connection

    @app.get("/connections")
    def catalog() -> dict[str, list[Connection]]:
        return {"connections": list(connections.values())}

    @app.get("/connections/{connection_id}")
    def resolve(connection_id: str) -> Connection:
        connection = connections.get(connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Unknown connection_id")
        return connection

    return app


app = create_registry_app()


def main() -> None:
    import os

    import uvicorn

    uvicorn.run(
        "tripity_experiment.registry:app",
        host=os.getenv("TRIPITY_REGISTRY_HOST", "0.0.0.0"),
        port=int(os.getenv("TRIPITY_REGISTRY_PORT", "9000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
