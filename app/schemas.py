import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import SandboxStatus, SandboxType


class SandboxCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo'd field is a 422, not silently ignored

    type: SandboxType
    # Bounded: no sandbox squats on the host forever, none flaps in and out in seconds.
    ttl_s: int = Field(default=600, ge=60, le=3600)


class SandboxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: SandboxType
    status: SandboxStatus
    url: str | None
    error: str | None
    attempts: int
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class SandboxAccepted(BaseModel):
    job_id: str
    sandbox_id: uuid.UUID
    type: SandboxType
    status: SandboxStatus
    expires_at: datetime
