"""
Схемы файлов (этап 4).

Тут только Response — Create-схемы нет, потому что файлы создаются
multipart/form-data через UploadFile, а не JSON-телом.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    uploaded_by: uuid.UUID | None
    original_filename: str
    content_type: str
    size_bytes: int
    created_at: datetime
