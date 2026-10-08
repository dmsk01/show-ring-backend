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

import uuid
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.file import FileVariant, UploadedFile
from app.models.user import User
from app.schemas.file import FileResponse, FileVariantResponse
from app.services import file_storage, upload_quota
from app.services.task_dispatch import create_and_enqueue_task
from app.services.task_queues import IMAGE_TASK_TYPE


router = APIRouter(prefix="/files", tags=["files"])

# Публичный файл/вариант по id неизменяем — кэш на год (ревью 2026-10-06, BE-24).
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"



async def _queue_image_processing(
    db: AsyncSession, file_id: uuid.UUID, user_id: uuid.UUID | None
) -> None:
    """
    Создаёт Task(process_image) и ставит его в очередь image_task через
    transactional outbox (ревью 2026-10-06, BE-19): недоступный RabbitMQ
    не теряет задачу и не валит загрузку.
    """
    await create_and_enqueue_task(
        db, type_=IMAGE_TASK_TYPE, payload={"file_id": str(file_id)},
        created_by=user_id,
    )


@router.post(
    "/upload",
    response_model=FileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Загрузить файл",
)
async def upload_file(
    file: UploadFile = File(...),
    # ИСПРАВЛЕНО (review 2026-06-10): folder — сырая строка, попадающая в
    # S3-ключ. Без валидации пользователь мог класть файлы в чужие
    # префиксы, плодить мусорные префиксы или ронять 500 строкой длиннее
    # String(512) на files.s3_key. Белый список вместо regex: префиксы
    # documents/ (приватные сгенерированные документы) и variants/
    # (превью от воркера) зарезервированы и в список не входят.
    folder: str = Query(
        "general", pattern="^(general|dogs|kennels|classifieds|posts)$"
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Квота тира до загрузки в S3: при превышении возвращаем 429/413 и
    # не тратим запись в MinIO. declared_size — точный размер из Starlette
    # (file.size), при отсутствии — консервативно потолок одного файла.
    declared_size = file.size or settings.max_upload_size_bytes
    try:
        await upload_quota.check_upload_quota(
            db, user, declared_size_bytes=declared_size
        )
    except upload_quota.UploadQuotaExceeded as e:
        return JSONResponse(
            status_code=e.status_code, content=e.body, headers=e.headers
        )

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
    # ИСПРАВЛЕНО (review 2026-06-10): варианты наследуют видимость
    # оригинала — иначе приватный image (скан родословной) был бы
    # доступен анонимно через варианты в обход ACL. Семантика та же,
    # что у GET /files/{id}: 404, не 403.
    db_file = await db.get(UploadedFile, file_id)
    if db_file is None or not db_file.is_public:
        raise HTTPException(status_code=404, detail="Файл не найден")
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
    # Видимость варианта = видимость оригинала (см. list_file_variants).
    db_file = await db.get(UploadedFile, variant.file_id)
    if db_file is None or not db_file.is_public:
        raise HTTPException(status_code=404, detail="Вариант не найден")
    body, content_type = await file_storage.get_file_stream(variant.s3_key)
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": "inline", "Cache-Control": _IMMUTABLE_CACHE},
    )


@router.get(
    "/{file_id}",
    summary="Скачать/показать файл",
    # response_model отключён: возвращаем raw bytes, не JSON.
    response_class=Response,
)
async def get_file(file_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    db_file = await db.get(UploadedFile, file_id)
    # is_public=False → приватный файл (сгенерированный документ с ПДн).
    # Отдаём 404 (а не 403), чтобы не раскрывать сам факт существования
    # файла анониму. Владелец/admin получают его через защищённый
    # /tasks/{id}/download. См. UploadedFile.is_public (review 2026-06-01).
    if db_file is None or not db_file.is_public:
        raise HTTPException(status_code=404, detail="Файл не найден")
    # Ревью 2026-10-06, BE-24: отдаём потоком, а не читаем файл (до 10 МБ)
    # целиком в память. Существование проверяем ДО ответа: ошибка внутри
    # генератора всплыла бы уже после отправки статуса 200.
    await file_storage.stat_file(db_file.s3_key)
    # ИСПРАВЛЕНО (bug_202): см. tasks.py — \r\n или " в original_filename
    # позволяли инжектировать произвольные HTTP-заголовки. RFC 6266
    # filename* = UTF-8''<percent-encoded> закрывает класс ошибки.
    safe_name = quote(db_file.original_filename or "file", safe="")
    return StreamingResponse(
        file_storage.iter_file(db_file.s3_key),
        media_type=db_file.content_type,
        headers={
            # inline — браузер отрендерит картинку (аватары/фото).
            "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
            # Содержимое по id неизменяемо (новая загрузка = новый id) —
            # кэшируем надолго, браузер и nginx не дёргают MinIO повторно.
            "Cache-Control": _IMMUTABLE_CACHE,
        },
    )
