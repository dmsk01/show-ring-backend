"""
Репозиторий блог-постов (этап 17).

Чтение всегда грузит author + author.profile одним selectinload (анти-N+1),
чтобы сборщик ответа (schemas.post.to_card/to_response) мог построить ФИО
автора без отдельного запроса на каждый пост.

Фильтр списка собирается динамически (условие добавляется только если
параметр задан) на SQLAlchemy Core — тот же паттерн, что в репозитории
объявлений (app/repositories/classified.py).
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.post import Post, PostPublish
from app.models.user import User

# Подгрузка автора и его профиля одним пакетом — нужна везде, где строим
# ответ (карточка/detail). Выносим в константу, чтобы не дублировать.
_AUTHOR_LOAD = selectinload(Post.author).selectinload(User.profile)


def _filter_stmt(publish: PostPublish | None, query: str | None):
    stmt = select(Post)
    if publish is not None:
        stmt = stmt.where(Post.publish == publish)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(
                Post.title.ilike(like),
                Post.description.ilike(like),
                # Поиск по тегу: query = ANY(tags). Точное совпадение тега,
                # не подстрока — теги это метки, а не свободный текст.
                Post.tags.any(query),
            )
        )
    return stmt


async def get_by_slug(db: AsyncSession, slug: str) -> Post | None:
    stmt = select(Post).where(Post.slug == slug).options(_AUTHOR_LOAD)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_by_id(db: AsyncSession, id_: uuid.UUID) -> Post | None:
    stmt = select(Post).where(Post.id == id_).options(_AUTHOR_LOAD)
    return (await db.execute(stmt)).scalar_one_or_none()


async def slug_exists(db: AsyncSession, slug: str) -> bool:
    row = (
        await db.execute(select(Post.id).where(Post.slug == slug).limit(1))
    ).scalar_one_or_none()
    return row is not None


async def list_page(
    db: AsyncSession,
    *,
    publish: PostPublish | None = None,
    query: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> Sequence[Post]:
    stmt = (
        _filter_stmt(publish, query)
        .order_by(Post.created_at.desc())
        .options(_AUTHOR_LOAD)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count(
    db: AsyncSession,
    *,
    publish: PostPublish | None = None,
    query: str | None = None,
) -> int:
    base = _filter_stmt(publish, query).subquery()
    return int(
        (await db.execute(select(func.count()).select_from(base))).scalar_one()
    )


async def related(
    db: AsyncSession, slug: str, *, limit: int = 4
) -> Sequence[Post]:
    """Последние опубликованные посты, КРОМЕ текущего (по slug)."""
    stmt = (
        select(Post)
        .where(Post.slug != slug, Post.publish == PostPublish.published)
        .order_by(Post.created_at.desc())
        .options(_AUTHOR_LOAD)
        .limit(limit)
    )
    return (await db.execute(stmt)).scalars().all()


async def create(db: AsyncSession, **fields) -> Post:
    obj = Post(**fields)
    db.add(obj)
    await db.flush()
    return obj
