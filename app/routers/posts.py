"""
Роутер блога (этап 17).

Read — публично (нужно для лендинга /post и SEO): список с пагинацией,
detail по чистому пути /posts/{slug}, «последние/похожие» — /posts/{slug}/
related. Write — под ролью admin/organizer (require_any_role): создание,
обновление, удаление. 401 без токена, 403 при нехватке прав.

Формы ответов — как у Dogs/Notifications: список это PostPage
(items/total/page/per_page), detail — объект PostResponse напрямую (без
{"post": …}). Перекладку под Minimal Kit делает фронт у себя.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.progressive_ban import check_rate_limit
from app.redis import get_redis
from app.dependencies import (
    get_current_user,
    get_current_user_optional,
    is_writer,
    require_any_role,
)
from app.models.post import PostPublish
from app.models.user import User
from app.repositories import post as repo
from app.schemas.post import (
    PostCard,
    PostCreate,
    PostPage,
    PostResponse,
    PostUpdate,
    to_card,
    to_response,
)
from app.services import post as svc

router = APIRouter(prefix="/posts", tags=["posts"])

# Write доступен admin/organizer. Один Depends переиспользуем во всех
# пишущих ручках (как dependencies=[...] в classifieds/admin-роутерах).
_require_writer = require_any_role("admin", "organizer")


@router.get("", response_model=PostPage, summary="Список постов (пагинация)")
async def list_posts(
    request: Request,
    publish: PostPublish | None = Query(
        None, description="Фильтр по статусу (published/draft)"
    ),
    query: str | None = Query(
        None, max_length=200, description="Поиск по title/description/тегам"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
    redis: Redis = Depends(get_redis),
):
    # Аудит M1: публичный список + ILIKE-поиск (?query) — DoS-вектор на
    # анонимной ручке. 60/мин на IP щедро для листания, режет флуд. Тот же
    # check_rate_limit, что у /classifieds/search (bug_213). fail-open: при
    # сбое Redis не ломаем публичную витрину (это не auth-ручка).
    await check_rate_limit(request, limit=60, window=60, redis=redis)
    # Аудит H1: публичная витрина отдаёт только published. Черновики видит
    # лишь writer (admin/organizer) и только если сам их запросил.
    if not is_writer(user):
        publish = PostPublish.published
    items = await repo.list_page(
        db, publish=publish, query=query, page=page, per_page=per_page
    )
    total = await repo.count(db, publish=publish, query=query)
    return PostPage(
        items=[to_card(p) for p in items],
        total=total,
        page=page,
        per_page=per_page,
    )


# Внимание: статический ничего не ломает — /{slug}/related имеет лишний
# сегмент, поэтому не конфликтует с /{slug}. А POST/PUT/DELETE — другие
# методы. Порядок регистрации тут не критичен.
@router.get(
    "/{slug}/related",
    response_model=list[PostCard],
    summary="Последние/похожие посты (кроме текущего)",
)
async def related_posts(
    slug: str,
    limit: int = Query(4, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    items = await repo.related(db, slug, limit=limit)
    return [to_card(p) for p in items]


@router.get("/{slug}", response_model=PostResponse, summary="Пост по slug")
async def get_post(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    post = await repo.get_by_slug(db, slug)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    # Аудит H1: черновик доступен только writer'у; анониму — как будто нет.
    if post.publish != PostPublish.published and not is_writer(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return to_response(post)


@router.post(
    "",
    response_model=PostResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_writer)],
    summary="Создать пост (admin/organizer)",
)
async def create_post(
    body: PostCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    post = await svc.create_post(db, body.model_dump(), author=user)
    return to_response(post)


@router.put(
    "/{post_id}",
    response_model=PostResponse,
    dependencies=[Depends(_require_writer)],
    summary="Обновить пост (admin/organizer)",
)
async def update_post(
    post_id: uuid.UUID,
    body: PostUpdate,
    db: AsyncSession = Depends(get_db),
):
    try:
        post = await svc.update_post(
            db, post_id, body.model_dump(exclude_unset=True)
        )
    except ValueError as e:
        code = status.HTTP_404_NOT_FOUND if str(e) == "not_found" else 400
        raise HTTPException(code, str(e))
    return to_response(post)


@router.delete(
    "/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_require_writer)],
    summary="Удалить пост (admin/organizer)",
)
async def delete_post(
    post_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    try:
        await svc.delete_post(db, post_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
