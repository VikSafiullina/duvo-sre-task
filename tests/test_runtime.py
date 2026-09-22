"""DockerRuntime against a stub Docker client: idempotency, hardening and readiness."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from docker.errors import APIError, NotFound

from app.config import Settings
from app.runtime import LABEL_EXPIRES, LABEL_ID, DockerRuntime, SandboxNotReady, container_name

EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)


class StubContainer:
    def __init__(self, name: str, status: str = "running", labels: dict[str, str] | None = None):
        self.id, self.name, self.status = uuid.uuid4().hex, name, status
        self.ports = {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49153"}]}
        self.attrs = {"Labels": labels or {}}
        self.removed = False

    def reload(self) -> None:
        pass

    def remove(self, force: bool = False) -> None:
        self.removed = True


class StubContainers:
    def __init__(self) -> None:
        self.by_name: dict[str, StubContainer] = {}
        self.runs: list[dict[str, Any]] = []
        self.run_error: Exception | None = None

    def get(self, name: str) -> StubContainer:
        if name not in self.by_name or self.by_name[name].removed:
            raise NotFound(name)
        return self.by_name[name]

    def run(self, image: str, **kwargs: Any) -> StubContainer:
        self.runs.append({"image": image, **kwargs})
        if self.run_error:
            raise self.run_error
        c = self.by_name[kwargs["name"]] = StubContainer(kwargs["name"])
        return c

    def list(self, **kwargs: Any) -> list[StubContainer]:
        return list(self.by_name.values())


class StubClient:
    def __init__(self) -> None:
        self.containers = StubContainers()


@pytest.fixture
def runtime(settings: Settings) -> DockerRuntime:
    return DockerRuntime(settings, client=StubClient())


def test_creates_hardened_labelled_container_on_loopback(runtime: DockerRuntime) -> None:
    sid = uuid.uuid4()
    assert runtime._ensure_container(sid, EXPIRES) == "49153"
    [run] = runtime.client.containers.runs
    assert run["name"] == container_name(sid)
    assert run["labels"] == {LABEL_ID: str(sid), LABEL_EXPIRES: str(int(EXPIRES.timestamp()))}
    assert run["ports"] == {"8080/tcp": ("127.0.0.1", None)}
    assert run["network"] == "duvo-sandboxes"
    assert (run["read_only"], run["cap_drop"], run["user"]) == (True, ["ALL"], "65534:65534")
    assert run["mem_limit"] and run["nano_cpus"] and run["pids_limit"]


def test_retry_reuses_running_container(runtime: DockerRuntime) -> None:
    sid = uuid.uuid4()
    runtime.client.containers.by_name[container_name(sid)] = StubContainer(container_name(sid))
    assert runtime._ensure_container(sid, EXPIRES) == "49153"
    assert runtime.client.containers.runs == []  # no second container


def test_exited_leftover_is_replaced(runtime: DockerRuntime) -> None:
    sid = uuid.uuid4()
    stale = StubContainer(container_name(sid), status="exited")
    runtime.client.containers.by_name[container_name(sid)] = stale
    runtime._ensure_container(sid, EXPIRES)
    assert stale.removed
    assert len(runtime.client.containers.runs) == 1


def test_lost_create_race_reuses_winner(runtime: DockerRuntime) -> None:
    sid = uuid.uuid4()
    containers = runtime.client.containers
    winner = StubContainer(container_name(sid))

    def racing_run(image: str, **kwargs: Any) -> StubContainer:
        containers.by_name[kwargs["name"]] = winner  # someone else created it first
        raise APIError("Conflict", response=type("R", (), {"status_code": 409})())

    containers.run = racing_run  # type: ignore[method-assign]
    assert runtime._ensure_container(sid, EXPIRES) == "49153"


async def test_remove_is_idempotent(runtime: DockerRuntime) -> None:
    sid = uuid.uuid4()
    runtime.client.containers.by_name[container_name(sid)] = StubContainer(container_name(sid))
    assert await runtime.remove(sid) is True
    assert await runtime.remove(sid) is False


async def test_list_reads_labels_and_skips_malformed(runtime: DockerRuntime) -> None:
    sid = uuid.uuid4()
    good = {LABEL_ID: str(sid), LABEL_EXPIRES: str(int(EXPIRES.timestamp()))}
    runtime.client.containers.by_name["a"] = StubContainer("a", labels=good)
    runtime.client.containers.by_name["b"] = StubContainer("b", labels={LABEL_ID: "not-a-uuid"})
    [found] = await runtime.list_sandboxes()
    assert (found.sandbox_id, found.expires_at) == (sid, EXPIRES)


async def test_launch_returns_public_url(
    runtime: DockerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ready(sandbox_id: uuid.UUID) -> None:
        pass

    monkeypatch.setattr(runtime, "_wait_ready", ready)
    assert await runtime.launch(uuid.uuid4(), EXPIRES) == "http://localhost:49153"


async def test_readiness_gives_up_at_deadline(settings: Settings) -> None:
    """Container up but server silent (here: name doesn't even resolve) -> bounded failure."""
    runtime = DockerRuntime(
        settings.model_copy(update={"sandbox_ready_timeout_s": 0.3}), client=StubClient()
    )
    start = datetime.now(UTC)
    with pytest.raises(SandboxNotReady):
        await runtime._wait_ready(uuid.uuid4())
    assert datetime.now(UTC) - start < timedelta(seconds=2)
