import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


def _str_enum(cls: type[enum.StrEnum]) -> Enum:
    """Store enums as plain VARCHAR values: adding a member needs no DB migration."""
    return Enum(cls, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    pass


class SandboxType(enum.StrEnum):
    HTTP = "http"


class Deployment(enum.StrEnum):
    """Worker pools. Each consumes its own queue; the producer splits jobs between them."""

    STABLE = "stable"
    CANARY = "canary"


class SandboxStatus(enum.StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


# Statuses that hold (or are about to hold) a container: they count against the cap.
ACTIVE_STATUSES = frozenset(
    {SandboxStatus.QUEUED, SandboxStatus.STARTING, SandboxStatus.RUNNING, SandboxStatus.STOPPING}
)


class Sandbox(Base):
    """An isolated environment an AI agent runs in. Postgres is the source of truth for its
    lifecycle; the queue only carries "go do this" messages."""

    __tablename__ = "sandboxes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    type: Mapped[SandboxType] = mapped_column(_str_enum(SandboxType))
    status: Mapped[SandboxStatus] = mapped_column(
        _str_enum(SandboxStatus), default=SandboxStatus.QUEUED
    )
    url: Mapped[str | None] = mapped_column(String(200))  # set once the server answers
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0)
    # Pool the start job was routed to (see app/rollout.py).
    deployment: Mapped[Deployment] = mapped_column(
        _str_enum(Deployment), default=Deployment.STABLE, server_default=Deployment.STABLE.value
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # TTL from creation
    # Client-supplied Idempotency-Key: a retried POST gets the original sandbox back instead
    # of a second one. Unique, so concurrent retries race safely on the database.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
