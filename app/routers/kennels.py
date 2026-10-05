"""
Роутер питомников (этап 4).
"""

from __future__ import annotations

import uuid
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import (
    get_current_user,
    get_current_user_optional,
    user_rate_limit,
)
from app.models.kennel import Kennel
from app.models.user import User
from app.utils.pagination import ANON_MAX_PER_PAGE, cap_per_page
from app.repositories import kennel as repo
from app.schemas.kennel import (
    KennelCreate,
    KennelPage,
    KennelResponse,
    KennelUpdate,
)
from app.services import consent as consent_svc
from app.services import kennel as svc
from app.utils.public_contacts import CONTACT_FIELDS, hide_private_contacts

router = APIRouter(prefix="/kennels", tags=["kennels"])


def _is_admin(user: User) -> bool:
    return any(r.role.value == "admin" for r in user.roles)


def _kennel_response(
    kennel: Kennel,
    dogs_count: int,
    litters_count: int,
    viewer: User | None,
) -> KennelResponse:
    """KennelResponse + агрегаты (is_verified тянется из ORM автоматически)."""
    resp = KennelResponse.model_validate(kennel)
    resp.dogs_count = dogs_count
    resp.litters_count = litters_count
    # Сайт питомника тоже может указывать на человека — скрываем вместе
    # с остальными контактами (ст. 10.1 152-ФЗ).
    return hide_private_contacts(
        resp,
        owner_id=kennel.owner_id,
        contacts_public=kennel.contacts_public,
        viewer=viewer,
        fields=(*CONTACT_FIELDS, "website"),
    )


async def _sync_contacts_consent(
    db: AsyncSession, request: Request, kennel: Kennel, owner: User
) -> None:
    """Журнал согласия на распространение — по фактическому флагу."""
    await consent_svc.set_publication_consent(
        db,
        kennel.owner_id,
        consent_svc.ConsentKind.public_kennel_contacts,
        kennel.id,
        kennel.contacts_public,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()


def _raise_for_error(err: ValueError) -> NoReturn:
    code = str(err)
    if code == "not_found":
        raise HTTPException(404, code)
    if code == "forbidden":
        raise HTTPException(403, code)
    if code == "duplicate_prefix":
        raise HTTPException(409, code)
    raise HTTPException(400, code)


@router.post(
    "",
    response_model=KennelResponse,
    dependencies=[Depends(user_rate_limit("create:kennel", limit=20, window=3600))],
    status_code=status.HTTP_201_CREATED,
    summary="Создать питомник",
)
async def create_kennel(
    request: Request,
    body: KennelCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # owner_id берём из текущего юзера — не доверяем клиенту.
    try:
        kennel = await svc.create_kennel(db, owner_id=user.id, **body.model_dump())
    except ValueError as e:
        _raise_for_error(e)
    if kennel.contacts_public:
        await _sync_contacts_consent(db, request, kennel, user)
    # Новый питомник — счётчики нулевые.
    return _kennel_response(kennel, 0, 0, user)


@router.get(
    "",
    response_model=KennelPage,
    summary="Список питомников",
)
async def list_kennels(
    city: str | None = Query(None),
    search: str | None = Query(None, max_length=128),
    sort_by: Literal["name", "created_at"] = Query("name"),
    order: Literal["asc", "desc"] = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    viewer: User | None = Depends(get_current_user_optional),
):
    per_page = cap_per_page(per_page, viewer, ANON_MAX_PER_PAGE)
    items = await repo.list_kennels(
        db, city=city, search=search, sort_by=sort_by, order=order,
        page=page, per_page=per_page,
    )
    # total — по тем же фильтрам (search, city), без offset/limit.
    total = await repo.count_kennels(db, city=city, search=search)
    counts = await repo.counts_by_kennels(db, [k.id for k in items])
    return KennelPage(
        items=[
            _kennel_response(k, *counts.get(k.id, (0, 0)), viewer)
            for k in items
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/{kennel_id}",
    response_model=KennelResponse,
    summary="Страница питомника",
)
async def get_kennel(
    kennel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    viewer: User | None = Depends(get_current_user_optional),
):
    obj = await repo.get_kennel(db, kennel_id)
    if obj is None:
        raise HTTPException(404, "Питомник не найден")
    counts = await repo.counts_by_kennels(db, [obj.id])
    return _kennel_response(obj, *counts.get(obj.id, (0, 0)), viewer)


@router.put(
    "/{kennel_id}",
    response_model=KennelResponse,
    summary="Обновить питомник",
)
async def update_kennel(
    request: Request,
    kennel_id: uuid.UUID,
    body: KennelUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fields = body.model_dump(exclude_unset=True)
    # Явный null не снимает флаг (колонка NOT NULL) — просто игнорируем.
    if fields.get("contacts_public", False) is None:
        fields.pop("contacts_public")
    if fields.get("contacts_public") is True:
        # Согласие на распространение даёт только сам субъект: админ может
        # скрыть контакты, но не опубликовать их за владельца.
        current = await repo.get_kennel(db, kennel_id)
        if current is not None and current.owner_id != user.id:
            raise HTTPException(403, "consent_owner_only")
    try:
        kennel = await svc.update_kennel(
            db,
            kennel_id=kennel_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            fields=fields,
        )
    except ValueError as e:
        _raise_for_error(e)
    if "contacts_public" in fields:
        await _sync_contacts_consent(db, request, kennel, user)
    counts = await repo.counts_by_kennels(db, [kennel.id])
    return _kennel_response(kennel, *counts.get(kennel.id, (0, 0)), user)


@router.delete(
    "/{kennel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить питомник",
)
async def delete_kennel(
    kennel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.delete_kennel(
            db,
            kennel_id=kennel_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
        )
    except ValueError as e:
        _raise_for_error(e)
