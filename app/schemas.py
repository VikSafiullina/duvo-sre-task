import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import ItemStatus


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: ItemStatus
    result: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class JobAccepted(BaseModel):
    job_id: str
    item_id: uuid.UUID
    status: ItemStatus
