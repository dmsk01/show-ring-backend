from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Event(BaseModel):
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    source: str = "api"


class CacheInvalidateEvent(Event):
    event_type: str = "cache.invalidate"


class BookCreatedEvent(Event):
    event_type: str = "book.created"
