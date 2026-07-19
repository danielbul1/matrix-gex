"""Local Docker orchestrator for self-hosted cloud browsers.

Loop 65: allocate one isolated Chromium stack per Tripity user without relying on
paid browser vendors. This module intentionally keeps the deployment primitive
simple: one Docker Compose project per user, each with:

- linuxserver/chromium for the remote browser UI + persistent profile;
- alpine/socat forwarding Chrome's container-local CDP port to a host-local port;
- all public bindings restricted to 127.0.0.1.

It is a local/single-host orchestrator, not the final production scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import shutil
import subprocess
import time
from typing import Sequence

from tripity_experiment.identity import User

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class DockerBrowserSpec:
    user_id: str
    public_id: str
    project_name: str
    profile_dir: Path
    compose_file: Path
    ui_port: int
    https_port: int
    cdp_port: int

    @property
    def login_url(self) -> str:
        return f"http://127.0.0.1:{self.ui_port}"

    @property
    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.cdp_port}"


class DockerCloudBrowserOrchestrator:
    """Generate/start/stop one local Chromium stack per user."""

    def __init__(
        self,
        root: str | Path,
        *,
        image: str = "lscr.io/linuxserver/chromium:latest",
        socat_image: str = "alpine/socat:latest",
        ui_port_base: int = 31000,
        https_port_base: int = 33000,
        cdp_port_base: int = 32000,
        port_span: int = 1000,
        password: str = "change-this-before-remote-use",
    ) -> None:
        self.root = Path(root)
        self.image = image
        self.socat_image = socat_image
        self.ui_port_base = ui_port_base
        self.https_port_base = https_port_base
        self.cdp_port_base = cdp_port_base
        self.port_span = port_span
        self.password = password

    def _slug(self, user: User) -> str:
        slug = _SAFE.sub("-", user.public_id).strip("-_.").lower()
        while ".." in slug:
            slug = slug.replace("..", ".")
        slug = slug.replace(".", "-").strip("-")
        return slug or hashlib.sha256(user.id.encode()).hexdigest()[:12]

    def _offset(self, user: User, salt: str) -> int:
        digest = hashlib.sha256(f"{salt}:{user.public_id}:{user.id}".encode()).digest()
        return int.from_bytes(digest[:4], "big") % self.port_span

    def spec_for(self, user: User) -> DockerBrowserSpec:
        slug = self._slug(user)
        user_root = self.root / slug
        return DockerBrowserSpec(
            user_id=user.id,
            public_id=user.public_id,
            project_name=f"tripity-browser-{slug}",
            profile_dir=user_root / "profile",
            compose_file=user_root / "docker-compose.yml",
            ui_port=self.ui_port_base + self._offset(user, "ui"),
            https_port=self.https_port_base + self._offset(user, "https"),
            cdp_port=self.cdp_port_base + self._offset(user, "cdp"),
        )

    def compose_text(self, spec: DockerBrowserSpec) -> str:
        # Deliberately bind host ports to 127.0.0.1. Public access should go
        # through Tripity's authenticated one-time login route/proxy, never by
        # exposing CDP or browser UI directly.
        return f"""services:
  browser:
    image: {self.image}
    security_opt:
      - seccomp:unconfined
    shm_size: "2gb"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
      - CUSTOM_USER=tripity
      - PASSWORD={self.password}
      - CHROME_CLI=--remote-debugging-address=0.0.0.0 --remote-debugging-port=9222 --disable-blink-features=AutomationControlled
    volumes:
      - ./profile:/config
    ports:
      - "127.0.0.1:{spec.ui_port}:3000"
      - "127.0.0.1:{spec.https_port}:3001"
      - "127.0.0.1:{spec.cdp_port}:9223"
    restart: unless-stopped

  cdp-forwarder:
    image: {self.socat_image}
    depends_on:
      - browser
    network_mode: "service:browser"
    command: "TCP-LISTEN:9223,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9222"
    restart: unless-stopped
"""

    def ensure_files(self, user: User) -> DockerBrowserSpec:
        spec = self.spec_for(user)
        spec.profile_dir.mkdir(parents=True, exist_ok=True)
        spec.compose_file.parent.mkdir(parents=True, exist_ok=True)
        spec.compose_file.write_text(self.compose_text(spec), encoding="utf-8")
        return spec

    def _last_used_file(self, spec: DockerBrowserSpec) -> Path:
        return spec.compose_file.parent / ".last_used"

    def touch(self, user: User, *, when: float | None = None) -> DockerBrowserSpec:
        spec = self.ensure_files(user)
        self._last_used_file(spec).write_text(str(time.time() if when is None else when), encoding="utf-8")
        return spec

    def last_used(self, user: User) -> float | None:
        spec = self.spec_for(user)
        path = self._last_used_file(spec)
        if not path.exists():
            return None
        try:
            return float(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _compose_cmd(self, spec: DockerBrowserSpec, args: Sequence[str]) -> list[str]:
        return [
            "docker",
            "compose",
            "-p",
            spec.project_name,
            "-f",
            str(spec.compose_file),
            *args,
        ]

    def start(self, user: User) -> DockerBrowserSpec:
        spec = self.touch(user)
        subprocess.run(self._compose_cmd(spec, ["up", "-d"]), check=True)
        return spec

    def stop(self, user: User) -> DockerBrowserSpec:
        spec = self.ensure_files(user)
        subprocess.run(self._compose_cmd(spec, ["down"]), check=True)
        return spec

    def delete(self, user: User) -> DockerBrowserSpec:
        """Stop the browser stack and delete generated files/profile for one user."""
        spec = self.ensure_files(user)
        subprocess.run(self._compose_cmd(spec, ["down", "--remove-orphans"]), check=True)
        shutil.rmtree(spec.compose_file.parent, ignore_errors=True)
        return spec

    def status(self, user: User) -> dict[str, object]:
        spec = self.spec_for(user)
        return {
            "project_name": spec.project_name,
            "profile_dir": str(spec.profile_dir),
            "profile_exists": spec.profile_dir.exists(),
            "login_url": spec.login_url,
            "cdp_endpoint": spec.cdp_endpoint,
            "last_used_at": self.last_used(user),
        }

    def hibernate_idle(self, users: Sequence[User], *, max_idle_seconds: float, now: float | None = None) -> list[DockerBrowserSpec]:
        """Stop idle browser stacks while preserving profiles and compose files."""
        now = time.time() if now is None else now
        stopped: list[DockerBrowserSpec] = []
        for user in users:
            last = self.last_used(user)
            if last is None or now - last < max_idle_seconds:
                continue
            spec = self.ensure_files(user)
            subprocess.run(self._compose_cmd(spec, ["down"]), check=True)
            stopped.append(spec)
        return stopped

    def ps(self, user: User) -> subprocess.CompletedProcess[str]:
        spec = self.ensure_files(user)
        return subprocess.run(
            self._compose_cmd(spec, ["ps"]),
            check=True,
            text=True,
            capture_output=True,
        )
