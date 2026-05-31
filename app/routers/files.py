"""
Эндпоинты загрузки и скачивания файлов (этап 4).

POST /files/upload  — авторизованный пользователь загружает файл,
                       получает file_id (UUID), который потом цепляет
                       к питомнику или собаке.
GET  /files/{id}    — публичный — браузер сразу может рендерить аватары.
                       Если файл должен быть приватным, в этапе 4
                       это не требуется (фото собак публичны по идее
                       платформы).
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.file import FileVariant, UploadedFile
from app.models.user import User
from app.repositories import task as task_repo
from app.schemas.file import FileResponse, FileVariantResponse
from app.schemas.task import TaskMessage
from app.services import file_storage
from app.services.rabbit import rabbit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

# Очередь и тип задачи обработки изображений (см. worker/main.py run_files).
IMAGE_TASK_QUEUE = "image_task"
IMAGE_TASK_TYPE = "process_image"


async def _queue_image_processing(
    db: AsyncSession, file_id: uuid.UUID, user_id: uuid.UUID | None
) -> None:
    """
    Создаёт Task(process_image) и публикует в image_task. Если RabbitMQ
    недоступен — не валим загрузку: задача осталась в БД (pending), её
    можно перепубликовать позже.
    """
    payload = {"file_id": str(file_id)}
    task = await task_repo.create_task(
        db, type_=IMAGE_TASK_TYPE, payload=payload, created_by=user_id
    )
    message = TaskMessage(
        task_id=task.id, action=IMAGE_TASK_TYPE, payload=payload
    ).to_json()
    try:
        await rabbit_service.publish(IMAGE_TASK_QUEUE, message)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to publish image task %s: %s", task.id, e)


@router.post(
    "/upload",
    response_model=FileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Загрузить файл",
)
async def upload_file(
    file: UploadFile = File(...),
    folder: str = "general",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Сначала валидируем + загружаем в S3, потом — пишем метаданные в БД.
    # Если в БД произойдёт сбой, файл-сирота в S3 потом подберёт cleanup-job
    # (этап 14, scheduled tasks).
    s3_key, ct, filename, size_bytes = await file_storage.upload_file(
        file, folder=folder
    )
    db_file = UploadedFile(
        uploaded_by=user.id,
        s3_key=s3_key,
        original_filename=filename,
        content_type=ct,
        size_bytes=size_bytes,
    )
    db.add(db_file)
    await db.commit()
    await db.refresh(db_file)
    # Изображения обрабатываем асинхронно: превью + средний с watermark.
    if ct.startswith("image/"):
        await _queue_image_processing(db, db_file.id, user.id)
    return db_file


@router.get(
    "/{file_id}/variants",
    response_model=list[FileVariantResponse],
    summary="Варианты изображения (превью/средний)",
)
async def list_file_variants(
    file_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    variants = (
        await db.execute(
            select(FileVariant)
            .where(FileVariant.file_id == file_id)
            .order_by(FileVariant.width.asc())
        )
    ).scalars().all()
    return list(variants)


@router.get(
    "/variants/{variant_id}",
    summary="Скачать/показать вариант изображения",
    response_class=Response,
)
async def get_file_variant(
    variant_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    variant = await db.get(FileVariant, variant_id)
    if variant is None:
        raise HTTPException(status_code=404, detail="Вариант не найден")
    body, content_type = await file_storage.get_file_stream(variant.s3_key)
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": "inline"},
    )


@router.get(
    "/{file_id}",
    summary="Скачать/показать файл",
    # response_model отключён: возвращаем raw bytes, не JSON.
    response_class=Response,
)
async def get_file(file_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    db_file = await db.get(UploadedFile, file_id)
    if db_file is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    body, content_type = await file_storage.get_file_stream(db_file.s3_key)
    # ИСПРАВЛЕНО (bug_202): см. tasks.py — \r\n или " в original_filename
    # позволяли инжектировать произвольные HTTP-заголовки. RFC 6266
    # filename* = UTF-8''<percent-encoded> закрывает класс ошибки.
    safe_name = quote(db_file.original_filename or "file", safe="")
    return Response(
        content=body,
        media_type=content_type,
        # Content-Disposition inline — браузер отрендерит картинку.
        # Если бы было attachment — скачался бы файлом. inline здесь
        # удобнее для аватаров/фото.
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
        },
    )
