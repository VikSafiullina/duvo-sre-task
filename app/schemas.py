import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import Deployment, SandboxStatus, SandboxType


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
    deployment: Deployment
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
    deployment: Deployment
    expires_at: datetime


class RolloutUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # strict: "50", 50.5 and true are 422s, not quietly coerced into a traffic shift.
    canary_weight: int = Field(ge=0, le=100, strict=True)


class RolloutOut(BaseModel):
    canary_weight: int
