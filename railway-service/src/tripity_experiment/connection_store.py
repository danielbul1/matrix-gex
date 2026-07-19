"""Persistent, owner-scoped connections with optional secret encryption."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet


@dataclass
class Connection:
    name: str
    openapi_url: str = ""
    base_url: str = ""
    operations: list[str] = field(default_factory=list)
    api_bearer_token: str | None = None
    owner_id: str = "legacy"
    kind: str = "openapi"
    mcp_url: str | None = None
    # For kind="browser": the site origin the AI gets full access to, run inside
    # the user's authenticated Chrome session.
    origin: str | None = None
    # For kind="browser": API endpoints discovered from the user's own browsing,
    # shown to the AI so it can pick the right path from a natural-language ask.
    endpoints: list[str] = field(default_factory=list)


class ConnectionStore:
    def __init__(self, path: str | Path, *, encryption_key: str | bytes | None = None) -> None:
        self.path = Path(path)
        if isinstance(encryption_key, str):
            encryption_key = encryption_key.encode()
        self._cipher = Fernet(encryption_key) if encryption_key else None

    def _decode(self, item: dict[str, object]) -> Connection:
        data = dict(item)
        encrypted = data.pop("api_bearer_token_encrypted", None)
        if encrypted is not None:
            if self._cipher is None:
                raise RuntimeError("TRIPITY_ENCRYPTION_KEY is required to read stored secrets")
            data["api_bearer_token"] = self._cipher.decrypt(str(encrypted).encode()).decode()
        return Connection(**data)  # type: ignore[arg-type]

    def _encode(self, connection: Connection) -> dict[str, object]:
        data = asdict(connection)
        token = data.pop("api_bearer_token")
        if token is not None:
            if self._cipher is None:
                # Backward-compatible local experiment mode. Multi-user mode
                # always supplies a key and therefore never takes this branch.
                data["api_bearer_token"] = token
            else:
                data["api_bearer_token_encrypted"] = self._cipher.encrypt(
                    str(token).encode()
                ).decode()
        return data

    def load(self, owner_id: str | None = None) -> list[Connection]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        connections = [self._decode(item) for item in raw]
        if owner_id is not None:
            connections = [c for c in connections if c.owner_id == owner_id]
        return connections

    def _write(self, connections: list[Connection]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([self._encode(c) for c in connections], indent=2),
            encoding="utf-8",
        )

    def add(self, connection: Connection) -> None:
        """Add or replace a connection (dedup by owner and name)."""
        connections = [
            c
            for c in self.load()
            if not (c.owner_id == connection.owner_id and c.name == connection.name)
        ]
        connections.append(connection)
        self._write(connections)

    def remove(self, owner_id: str, name: str) -> bool:
        """Delete one of an owner's connections by name. Returns True if removed.

        Deletion is an explicit user action (in the portal), never a side effect
        of disconnecting Tripity from an AI — the data lives here in the hub.
        """
        connections = self.load()
        kept = [
            c for c in connections if not (c.owner_id == owner_id and c.name == name)
        ]
        if len(kept) == len(connections):
            return False
        self._write(kept)
        return True

    def remove_all(self, owner_id: str) -> int:
        """Delete every connection owned by ``owner_id``. Returns how many."""
        connections = self.load()
        kept = [c for c in connections if c.owner_id != owner_id]
        removed = len(connections) - len(kept)
        if removed:
            self._write(kept)
        return removed
