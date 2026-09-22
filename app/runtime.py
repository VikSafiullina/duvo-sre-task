"""Sandbox runtime: one Docker container per sandbox, driven over the Docker API.

The Docker SDK is synchronous, so every call runs in a thread with two bounds: the SDK's own
per-request HTTP timeout and an asyncio deadline over the whole sequence. Containers carry
labels (sandbox id, expiry) so the reaper can find every sandbox the daemon is running, even
ones the database lost track of.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol

import docker
import httpx
from docker.errors import APIError, NotFound

from app.config import Settings

log = logging.getLogger("app.runtime")

LABEL_ID = "duvo.sandbox.id"
LABEL_EXPIRES = "duvo.sandbox.expires_at"
_PULL_DEADLINE_S = 120.0


class SandboxNotReady(RuntimeError):
    """The container started but its server never answered."""


@dataclass(frozen=True)
class RuntimeSandbox:
    sandbox_id: uuid.UUID
    expires_at: datetime


class SandboxRuntime(Protocol):
    async def launch(self, sandbox_id: uuid.UUID, expires_at: datetime) -> str: ...
    async def remove(self, sandbox_id: uuid.UUID) -> bool: ...
    async def list_sandboxes(self) -> list[RuntimeSandbox]: ...


def container_name(sandbox_id: uuid.UUID) -> str:
    return f"sandbox-{sandbox_id}"


class DockerRuntime:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        # Building the client asks the daemon for its API version: fails fast at worker
        # startup if the socket is missing, instead of failing every job later.
        self.client = client or docker.from_env(timeout=settings.docker_timeout_s)

    async def _run[T](self, fn: Callable[[], T], deadline_s: float | None = None) -> T:
        async with asyncio.timeout(deadline_s or self.settings.docker_timeout_s * 3):
            return await asyncio.to_thread(fn)

    async def prepare(self) -> None:
        """Pull the image once at startup so the first sandbox doesn't spend its start
        budget downloading it. Failure is not fatal: `containers.run` pulls on demand."""
        image = self.settings.sandbox_image
        try:
            await self._run(partial(self.client.images.get, image))
        except NotFound:
            log.info("pulling sandbox image", extra={"image": image})
            try:
                await self._run(partial(self.client.images.pull, image), _PULL_DEADLINE_S)
            except Exception as exc:
                log.warning("image pre-pull failed", extra={"image": image, "error": repr(exc)})

    async def launch(self, sandbox_id: uuid.UUID, expires_at: datetime) -> str:
        host_port = await self._run(partial(self._ensure_container, sandbox_id, expires_at))
        await self._wait_ready(sandbox_id)
        return f"http://{self.settings.sandbox_public_host}:{host_port}"

    async def remove(self, sandbox_id: uuid.UUID) -> bool:
        """Idempotent: True if a container was removed, False if there was none."""
        return await self._run(partial(self._remove, container_name(sandbox_id)))

    async def list_sandboxes(self) -> list[RuntimeSandbox]:
        # sparse=True: one list call instead of one inspect per container
        containers = await self._run(
            partial(self.client.containers.list, all=True, sparse=True, filters={"label": LABEL_ID})
        )
        found = []
        for c in containers:
            labels = c.attrs.get("Labels") or {}
            try:
                expires = datetime.fromtimestamp(int(labels[LABEL_EXPIRES]), UTC)
                found.append(RuntimeSandbox(uuid.UUID(labels[LABEL_ID]), expires))
            except (KeyError, ValueError):
                log.warning("container with malformed sandbox labels", extra={"id": c.id})
        return found

    def _ensure_container(self, sandbox_id: uuid.UUID, expires_at: datetime) -> str:
        """Create-or-reuse by deterministic name, so a job retried after a worker crash picks
        up the container the first attempt made instead of leaking a second one."""
        s = self.settings
        name = container_name(sandbox_id)
        container = self._get(name)
        if container is not None and container.status != "running":
            container.remove(force=True)  # created-but-never-started or exited: start over
            container = None
        if container is None:
            try:
                container = self.client.containers.run(
                    s.sandbox_image,
                    command=["--port", str(s.sandbox_port)],
                    name=name,
                    detach=True,
                    labels={
                        LABEL_ID: str(sandbox_id),
                        LABEL_EXPIRES: str(int(expires_at.timestamp())),
                    },
                    network=s.sandbox_network,
                    ports={f"{s.sandbox_port}/tcp": ("127.0.0.1", None)},  # loopback, random port
                    # Hardening: agents' code is untrusted. Not a real isolation boundary (see
                    # README) but closes the cheap doors.
                    user="65534:65534",
                    read_only=True,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    mem_limit=s.sandbox_memory,
                    nano_cpus=int(s.sandbox_cpus * 1e9),
                    pids_limit=s.sandbox_pids,
                )
            except APIError as exc:
                if exc.status_code != 409:
                    raise
                container = self.client.containers.get(name)  # lost a create race: reuse
        container.reload()  # port bindings are only known after start
        bindings = container.ports.get(f"{s.sandbox_port}/tcp") or []
        if not bindings:
            raise RuntimeError(f"{name}: no host port published")
        return bindings[0]["HostPort"]

    def _get(self, name: str) -> Any:
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def _remove(self, name: str) -> bool:
        try:
            self.client.containers.get(name).remove(force=True)
        except NotFound:
            return False
        return True

    async def _wait_ready(self, sandbox_id: uuid.UUID) -> None:
        """Probe over the private sandbox network (the worker is attached to it)."""
        url = f"http://{container_name(sandbox_id)}:{self.settings.sandbox_port}/"
        try:
            async with (
                asyncio.timeout(self.settings.sandbox_ready_timeout_s),
                httpx.AsyncClient(timeout=1.0) as http,
            ):
                while True:
                    try:
                        if (await http.get(url)).status_code < 500:
                            return
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.2)
        except TimeoutError:
            raise SandboxNotReady(
                f"no answer on {url} within {self.settings.sandbox_ready_timeout_s}s"
            ) from None
