"""
Файловое хранилище через MinIO/S3 (этап 4).

Архитектура:
- Используем aioboto3 — async-обёртку над boto3. Не делаем sync-вызовы
  в FastAPI: иначе блокируем event loop при загрузке больших файлов.
- Singleton-сессия (один aioboto3.Session() на процесс), client
  создаётся per-request — это рекомендация aiobotocore: client держит
  TCP-соединения, лучше открывать как async-context.
- Magic bytes валидация: проверяем СИГНАТУРУ файла (первые байты),
  а не доверяем Content-Type, который клиент может подделать.

Поддерживаемые форматы (этап 4 — фото и документы):
- JPEG: FF D8 FF
- PNG:  89 50 4E 47 0D 0A 1A 0A
- GIF:  GIF87a / GIF89a
- WebP: RIFF....WEBP
- PDF:  25 50 44 46 (%PDF)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, BinaryIO, cast

import aioboto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, UploadFile, status

from app.config import settings

logger = logging.getLogger(__name__)


# (signature_bytes, offset, content_type, extension)
# offset=0 для большинства, для WebP сигнатура начинается с RIFF (offset 0)
# + "WEBP" (offset 8) — поэтому отдельная функция.
_MAGIC_BYTES: list[tuple[bytes, str, str]] = [
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"%PDF", "application/pdf", "pdf"),
]


@dataclass
class DetectedFileType:
    content_type: str
    extension: str


def _detect_file_type(head: bytes) -> DetectedFileType | None:
    """
    По первым ~16 байтам определяем реальный формат. None — формат
    не распознан / не разрешён. Возвращаем именно content_type из
    магических байтов, а не из header'а — клиент мог подделать заголовок.
    """
    for sig, ct, ext in _MAGIC_BYTES:
        if head.startswith(sig):
            return DetectedFileType(content_type=ct, extension=ext)
    # WebP: первые 4 байта "RIFF", потом 4 байта длины, потом "WEBP".
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return DetectedFileType(content_type="image/webp", extension="webp")
    return None


# Singleton-сессия aioboto3 на процесс. Сессия безопасна для повторного
# использования между корутинами — она не держит соединений сама.
_session = aioboto3.Session()

# Backpressure: ограничивает число одновременных загрузок в MinIO на
# процесс. Создаётся на уровне модуля (лениво привязывается к loop'у при
# первом acquire в Python 3.10+).
_upload_semaphore = asyncio.Semaphore(settings.upload_max_concurrency)


def _s3_client() -> AbstractAsyncContextManager[Any]:
    """
    Контекстный менеджер с S3-клиентом. Использование:
        async with _s3_client() as s3:
            await s3.put_object(...)
    Каждый клиент закрывает свои TCP-коннекты при выходе из контекста.

    cast нужен, потому что aioboto3 Session.client() имеет неполные
    type-stubs и pyright видит его как абстрактный объект без
    __aenter__/__aexit__. Реально это AbstractAsyncContextManager —
    приводим тип явно, чтобы все `async with _s3_client()` ниже
    проходили проверку типов.
    """
    return cast(
        AbstractAsyncContextManager[Any],
        _session.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        ),
    )


async def upload_file(
    upload: UploadFile,
    *,
    folder: str = "general",
) -> tuple[str, str, str, int]:
    """
    Backpressure-обёртка над _upload_to_s3: захватывает семафор с
    таймаутом (при перегрузке — 503), затем делегирует загрузку и
    гарантированно освобождает слот в finally.
    """
    try:
        await asyncio.wait_for(
            _upload_semaphore.acquire(),
            timeout=settings.upload_acquire_timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Хранилище временно перегружено, повторите позже",
            headers={"Retry-After": "5"},
        )
    try:
        return await _upload_to_s3(upload, folder=folder)
    finally:
        _upload_semaphore.release()


async def _upload_to_s3(
    upload: UploadFile,
    *,
    folder: str = "general",
) -> tuple[str, str, str, int]:
    """
    Стримит файл в MinIO, валидирует размер и magic bytes.
    Возвращает (s3_key, content_type, original_filename, size_bytes).

    Стратегия чтения:
    - Читаем первый чанк (16 байт), валидируем magic bytes.
    - Если ОК — сбрасываем seek в начало и стримим в S3.
    - Размер файла ограничиваем счётчиком в процессе чтения, чтобы не
      словить OOM на гигабайтном загрузе.
    """
    head = await upload.read(16)
    detected = _detect_file_type(head)
    if detected is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Неподдерживаемый формат файла",
        )

    # Возвращаемся в начало — нам нужно загрузить файл целиком.
    await upload.seek(0)

    # Читаем чанками и считаем размер на лету, чтобы оборвать загрузку
    # ДО полного вычитывания, если файл превысил лимит (защита от
    # OOM/диск-флуда на гигабайтном загрузе). После проверки склеиваем
    # тело и кладём одним put_object — при лимите 10 МБ это допустимо.
    # ВНИМАНИЕ: само тело при этом всё же лежит в памяти целиком (до
    # 10 МБ на загрузку); честный потоковый upload_fileobj/multipart —
    # follow-up, когда поднимем лимит размера файла.
    chunks: list[bytes] = []
    total = 0
    chunk_size = 64 * 1024
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_upload_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Файл больше лимита "
                    f"{settings.max_upload_size_bytes} байт"
                ),
            )
        chunks.append(chunk)

    body = b"".join(chunks)
    s3_key = f"{folder}/{uuid.uuid4()}.{detected.extension}"

    try:
        async with _s3_client() as s3:
            await s3.put_object(
                Bucket=settings.s3_bucket,
                Key=s3_key,
                Body=body,
                ContentType=detected.content_type,
            )
    except ClientError as e:
        logger.error("S3 upload failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Файловое хранилище недоступно",
        )

    return s3_key, detected.content_type, upload.filename or "file", total


async def upload_bytes(
    content: bytes,
    *,
    content_type: str,
    extension: str,
    folder: str = "documents",
) -> tuple[str, int]:
    """
    Загружает уже готовые байты в MinIO (без UploadFile / валидации
    magic bytes).

    Используется фоновым воркером для сохранения сгенерированных PDF.
    Magic bytes-проверка тут не нужна — содержимое мы сами сформировали,
    оно гарантированно валидное.

    Возвращает (s3_key, size_bytes). Регистрацию UploadedFile делает
    вызывающий код, потому что у воркера есть свои сессии БД.
    """
    s3_key = f"{folder}/{uuid.uuid4()}.{extension}"
    try:
        async with _s3_client() as s3:
            await s3.put_object(
                Bucket=settings.s3_bucket,
                Key=s3_key,
                Body=content,
                ContentType=content_type,
            )
    except ClientError as e:
        logger.error("S3 upload (bytes) failed: %s", e)
        raise
    return s3_key, len(content)


async def get_file_stream(s3_key: str):
    """
    Возвращает (body_bytes, content_type) — файл читается ЦЕЛИКОМ в память.

    Подходит для небольших объектов и потребителей, которым нужны байты
    целиком: воркер обработки изображений (Pillow требует полный буфер),
    inline-отдача аватаров/фото. Для больших файлов (каталог выставки)
    используйте stat_file + iter_file со StreamingResponse — они стримят
    чанками и не держат файл в памяти.
    """
    try:
        async with _s3_client() as s3:
            obj = await s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
            # boto3 возвращает StreamingBody — читаем целиком.
            body = await obj["Body"].read()
            return body, obj.get("ContentType", "application/octet-stream")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise HTTPException(status_code=404, detail="Файл не найден")
        logger.error("S3 download failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Файловое хранилище недоступно",
        )


async def stat_file(s3_key: str) -> None:
    """
    Проверяет существование объекта через head_object: 404 если нет,
    503 при недоступности хранилища.

    Зачем отдельно от iter_file: ошибка, поднятая ВНУТРИ async-генератора
    iter_file, всплывёт уже после того, как StreamingResponse отправил
    клиенту статус 200 и заголовки — превратить её в чистый 404 уже
    нельзя. Поэтому существование проверяем заранее, до возврата
    StreamingResponse.
    """
    try:
        async with _s3_client() as s3:
            await s3.head_object(Bucket=settings.s3_bucket, Key=s3_key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in (
            "NoSuchKey", "404", "NotFound",
        ):
            raise HTTPException(status_code=404, detail="Файл не найден")
        logger.error("S3 head failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Файловое хранилище недоступно",
        )


async def iter_file(s3_key: str, chunk_size: int = 64 * 1024):
    """
    Async-генератор: стримит объект из S3 чанками, не загружая его целиком
    в память. Возвращает только тело; content_type для StreamingResponse
    берётся из БД (UploadedFile.content_type), а существование заранее
    проверяется через stat_file.

    S3-клиент держится открытым внутри генератора всё время стрима —
    нельзя обернуть его контекст вокруг возврата StreamingResponse, т.к.
    тело отдаётся уже после выхода из обработчика.
    """
    async with _s3_client() as s3:
        obj = await s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
        async for chunk in obj["Body"].iter_chunks(chunk_size):
            yield chunk


async def delete_file(s3_key: str) -> None:
    """
    Удаление из S3 — best-effort: ошибку логируем, но не пробрасываем,
    потому что хуже оставить запись в БД с висячим ключом.
    Параллельно с записью БД может быть выполнен idempotent retry.
    """
    try:
        async with _s3_client() as s3:
            await s3.delete_object(Bucket=settings.s3_bucket, Key=s3_key)
    except ClientError as e:
        logger.warning("S3 delete failed for %s: %s", s3_key, e)
