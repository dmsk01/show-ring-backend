"""
Unit: приватность и отдача загруженных изображений (ревью 2026-10-06, BE-24).

- Оригинал фото отдаётся публично по /files/{id}; раньше в нём оставались
  EXIF-метаданные, включая GPS-координаты съёмки (часто — адрес питомника
  или дома). Теперь метаданные удаляются при загрузке, ориентация
  применяется к пикселям.
- /files/{id} отдаёт файл потоком (stat_file + iter_file), а не читает
  его целиком в память, и с долгим кэшем: файл по id неизменяем.
"""

from __future__ import annotations

import io
import uuid
from contextlib import asynccontextmanager

from fastapi import UploadFile
from PIL import Image

from app.services import file_storage
from app.utils.image_processing import strip_image_metadata

_GPS_IFD = 0x8825
_ORIENTATION = 0x0112


def _jpeg_with_gps(orientation: int = 1) -> bytes:
    img = Image.new("RGB", (40, 20), (200, 100, 50))
    exif = Image.Exif()
    exif[_ORIENTATION] = orientation
    exif[0x010F] = "PhoneMaker"  # Make
    exif.get_ifd(_GPS_IFD).update({1: "N", 2: (55.0, 45.0, 0.0)})
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def test_strip_removes_exif_and_gps():
    raw = _jpeg_with_gps()
    assert Image.open(io.BytesIO(raw)).getexif()  # исходник с EXIF
    cleaned = strip_image_metadata(raw, "image/jpeg")
    exif = Image.open(io.BytesIO(cleaned)).getexif()
    assert not exif.get_ifd(_GPS_IFD)
    assert 0x010F not in exif


def test_strip_applies_orientation():
    # Orientation=6 — «повернуть на 90°»: после очистки тега картинка
    # должна быть повёрнута в пикселях, иначе отобразится боком.
    cleaned = strip_image_metadata(_jpeg_with_gps(orientation=6), "image/jpeg")
    assert Image.open(io.BytesIO(cleaned)).size == (20, 40)


def test_strip_keeps_non_images_untouched():
    pdf = b"%PDF-1.4 test"
    assert strip_image_metadata(pdf, "application/pdf") == pdf


class _FakeS3:
    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    async def put_object(self, **kwargs):
        self.bodies.append(kwargs["Body"])


async def test_upload_strips_metadata_before_storing(monkeypatch):
    fake = _FakeS3()

    @asynccontextmanager
    async def _client():
        yield fake

    monkeypatch.setattr(file_storage, "_s3_client", _client)
    upload = UploadFile(file=io.BytesIO(_jpeg_with_gps()), filename="dog.jpg")
    await file_storage.upload_file(upload, folder="dogs")
    stored = Image.open(io.BytesIO(fake.bodies[0])).getexif()
    assert not stored.get_ifd(_GPS_IFD)


async def test_public_file_is_streamed_with_cache_headers(client, db_session, monkeypatch):
    from unittest.mock import AsyncMock

    from app.models.file import UploadedFile

    f = UploadedFile(
        uploaded_by=None, s3_key=f"dogs/{uuid.uuid4()}.jpg",
        original_filename="dog.jpg", content_type="image/jpeg",
        size_bytes=6, is_public=True,
    )
    db_session.add(f)
    await db_session.commit()

    async def _iter(key, chunk_size=64 * 1024):
        yield b"abc"
        yield b"def"

    monkeypatch.setattr(file_storage, "stat_file", AsyncMock())
    monkeypatch.setattr(file_storage, "iter_file", _iter)
    monkeypatch.setattr(
        file_storage, "get_file_stream",
        AsyncMock(side_effect=AssertionError("файл не должен читаться целиком")),
    )
    r = await client.get(f"/files/{f.id}")
    assert r.status_code == 200, r.text
    assert r.content == b"abcdef"
    assert r.headers["content-type"] == "image/jpeg"
    assert "immutable" in r.headers["cache-control"]
