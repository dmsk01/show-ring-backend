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


class FileVariantResponse(BaseModel):
    """Обработанный вариант изображения (превью/средний). Байты —
    через GET /files/variants/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    content_type: str
    width: int
    height: int
    has_watermark: bool
    size_bytes: int
