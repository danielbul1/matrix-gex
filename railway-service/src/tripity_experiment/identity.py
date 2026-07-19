"""Small replaceable identity store for proving personal ownership.

This is deliberately not a production IdP. Passwords and access tokens are
stored only as salted hashes, and the rest of Tripity depends on user IDs
rather than on this implementation.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import uuid


def _hash_secret(secret: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 600_000)
    return base64.urlsafe_b64encode(digest).decode()


def _tokens_of(record: dict) -> list[dict[str, str]]:
    """Live token {salt, hash} pairs for a record, migrating the legacy single
    token_salt/token_hash fields into the list form."""
    tokens = list(record.get("tokens") or [])
    if record.get("token_hash") and record.get("token_salt"):
        tokens.append({"salt": record["token_salt"], "hash": record["token_hash"]})
    return tokens


@dataclass(frozen=True)
class User:
    id: str
    username: str
    public_id: str


@dataclass(frozen=True)
class Login:
    user: User
    access_token: str


class IdentityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        records = json.loads(self.path.read_text(encoding="utf-8"))
        changed = False
        for record in records:
            if not record.get("public_id"):
                record["public_id"] = secrets.token_urlsafe(12)
                changed = True
        if changed:
            self._save(records)
        return records

    def _save(self, records: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    def register(self, username: str, password: str) -> User:
        username = username.strip().casefold()
        if not username or len(password) < 8:
            raise ValueError("Username is required and password must be at least 8 characters")
        records = self._load()
        if any(record["username"] == username for record in records):
            raise ValueError("Username already exists")
        salt = secrets.token_bytes(16)
        user = User(
            id=uuid.uuid4().hex,
            username=username,
            public_id=secrets.token_urlsafe(12),
        )
        records.append(
            {
                **asdict(user),
                "password_salt": base64.urlsafe_b64encode(salt).decode(),
                "password_hash": _hash_secret(password, salt),
                # A user has one identity but may hold several live tokens at once
                # (their browser extension and an AI client are two sessions);
                # issuing a token must not evict the others.
                "tokens": [],
            }
        )
        self._save(records)
        return user

    def _issue_token_for_record(self, record: dict[str, str]) -> Login:
        token = secrets.token_urlsafe(32)
        token_salt = secrets.token_bytes(16)
        tokens = _tokens_of(record)
        tokens.append(
            {
                "salt": base64.urlsafe_b64encode(token_salt).decode(),
                "hash": _hash_secret(token, token_salt),
            }
        )
        # Bound growth; keep the most recent sessions.
        record["tokens"] = tokens[-10:]
        record.pop("token_salt", None)
        record.pop("token_hash", None)
        return Login(
            User(record["id"], record["username"], record["public_id"]),
            token,
        )

    def login(self, username: str, password: str) -> Login:
        records = self._load()
        username = username.strip().casefold()
        for record in records:
            if record["username"] != username:
                continue
            salt = base64.urlsafe_b64decode(record["password_salt"])
            if not hmac.compare_digest(record["password_hash"], _hash_secret(password, salt)):
                break
            login = self._issue_token_for_record(record)
            self._save(records)
            return login
        raise ValueError("Invalid username or password")

    def issue_token(self, user_id: str) -> Login:
        """Issue an additional access token for an already-authenticated user.

        Used by the OAuth authorization-code flow after the resource owner has
        approved an AI client. It preserves the multi-session behavior: browser
        extension, AI clients and portal sessions can all remain live together.
        """
        records = self._load()
        for record in records:
            if record["id"] == user_id:
                login = self._issue_token_for_record(record)
                self._save(records)
                return login
        raise ValueError("Unknown user")

    def authenticate(self, access_token: str) -> User | None:
        for record in self._load():
            for entry in _tokens_of(record):
                salt = base64.urlsafe_b64decode(entry["salt"])
                if hmac.compare_digest(entry["hash"], _hash_secret(access_token, salt)):
                    return User(record["id"], record["username"], record["public_id"])
        return None

    def users(self) -> list[User]:
        return [
            User(record["id"], record["username"], record["public_id"])
            for record in self._load()
        ]

    def delete(self, user_id: str) -> bool:
        """Delete a user by id. Returns True if a user was removed.

        The caller is responsible for also removing the user's connections; this
        only removes the identity record.
        """
        records = self._load()
        kept = [record for record in records if record["id"] != user_id]
        if len(kept) == len(records):
            return False
        self._save(kept)
        return True
