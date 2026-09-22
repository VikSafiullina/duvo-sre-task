import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import SandboxStatus, SandboxType


class SandboxCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo'd field is a 422, not silently ignored

    type: SandboxType


class SandboxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: SandboxType
    status: SandboxStatus
    url: str | None
    error: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class SandboxAccepted(BaseModel):
    job_id: str
    sandbox_id: uuid.UUID
    type: SandboxType
    status: SandboxStatus
