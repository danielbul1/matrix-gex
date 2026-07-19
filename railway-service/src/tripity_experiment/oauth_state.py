"""OAuth provider whose clients and tokens survive restarts via an encrypted file."""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.fernet import Fernet
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull


class PersistentOAuthProvider(InMemoryOAuthProvider):
    """InMemoryOAuthProvider that writes its state to an encrypted file.

    Every mutating OAuth step (client registration, authorization, token
    issuance, rotation, revocation) rewrites the state file, so a restarted
    sidecar keeps serving tokens it issued before the restart.
    """

    def __init__(
        self,
        state_path: str | Path,
        encryption_key: str | bytes,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._state_path = Path(state_path)
        if isinstance(encryption_key, str):
            encryption_key = encryption_key.encode()
        self._cipher = Fernet(encryption_key)
        self._restore()

    def _restore(self) -> None:
        if not self._state_path.exists():
            return
        state = json.loads(self._cipher.decrypt(self._state_path.read_bytes()))
        self.clients = {
            key: OAuthClientInformationFull.model_validate(value)
            for key, value in state["clients"].items()
        }
        self.auth_codes = {
            key: AuthorizationCode.model_validate(value)
            for key, value in state["auth_codes"].items()
        }
        self.access_tokens = {
            key: AccessToken.model_validate(value)
            for key, value in state["access_tokens"].items()
        }
        self.refresh_tokens = {
            key: RefreshToken.model_validate(value)
            for key, value in state["refresh_tokens"].items()
        }
        self._access_to_refresh_map = dict(state["access_to_refresh"])
        self._refresh_to_access_map = dict(state["refresh_to_access"])

    def _save(self) -> None:
        state = {
            "clients": {
                key: value.model_dump(mode="json") for key, value in self.clients.items()
            },
            "auth_codes": {
                key: value.model_dump(mode="json")
                for key, value in self.auth_codes.items()
            },
            "access_tokens": {
                key: value.model_dump(mode="json")
                for key, value in self.access_tokens.items()
            },
            "refresh_tokens": {
                key: value.model_dump(mode="json")
                for key, value in self.refresh_tokens.items()
            },
            "access_to_refresh": self._access_to_refresh_map,
            "refresh_to_access": self._refresh_to_access_map,
        }
        payload = self._cipher.encrypt(json.dumps(state).encode())
        temp_path = self._state_path.with_name(self._state_path.name + ".tmp")
        temp_path.write_bytes(payload)
        temp_path.replace(self._state_path)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await super().register_client(client_info)
        self._save()

    async def authorize(self, client, params) -> str:
        redirect = await super().authorize(client, params)
        self._save()
        return redirect

    async def exchange_authorization_code(self, client, authorization_code):
        token = await super().exchange_authorization_code(client, authorization_code)
        self._save()
        return token

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        token = await super().exchange_refresh_token(client, refresh_token, scopes)
        self._save()
        return token

    async def revoke_token(self, token) -> None:
        await super().revoke_token(token)
        self._save()
