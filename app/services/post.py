"""
Сервис блог-постов (этап 17).

Бизнес-правила:
- content санитизируется allowlist-bleach (app/utils/html_sanitize.py) на
  КАЖДОЙ записи — фронту не доверяем, XSS режется на бэке.
- slug генерируется из title (транслит кириллицы + уникальность) при
  создании и при смене title — фронт slug не присылает.
- author_id = текущий пользователь (берётся из current_user, не от клиента).

Право на write проверяется на уровне роутера (require_any_role admin/
organizer), сюда заходит уже авторизованный автор.

Технический долг: серверная валидация формы (content ≥ 100 символов,
tags ≥ 2, meta_keywords ≥ 1) — пока на стороне фронта; реальные
комментарии/избранное и инкремент total_views — см. план этапа.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.post import Post
from app.models.user import User
from app.repositories import post as repo
from app.utils.html_sanitize import sanitize_post_html
from app.utils.slug import make_slug


async def _unique_slug(
    db: AsyncSession, title: str, *, exclude_id: uuid.UUID | None = None
) -> str:
    """Уникальный slug из title. exclude_id — чтобы при обновлении пост не
    конфликтовал сам с собой (если title не поменялся фактически)."""

    async def exists(candidate: str) -> bool:
        existing = await repo.get_by_slug(db, candidate)
        return existing is not None and existing.id != exclude_id

    return await make_slug(title, exists)


async def create_post(db: AsyncSession, fields: dict, author: User) -> Post:
    data = dict(fields)
    data["content"] = sanitize_post_html(data.get("content") or "")
    data["slug"] = await _unique_slug(db, data["title"])
    data["author_id"] = author.id
    obj = await repo.create(db, **data)
    await db.commit()
    # Перечитываем с author+profile: commit экспайрит атрибуты, а сборщику
    # ответа нужен загруженный автор (иначе ленивый доступ → MissingGreenlet).
    reloaded = await repo.get_by_id(db, obj.id)
    assert reloaded is not None  # invariant: только что создали — точно есть
    return reloaded


async def update_post(
    db: AsyncSession,
    post_id: uuid.UUID,
    fields: dict,
    *,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> Post:
    obj = await repo.get_by_id(db, post_id)
    if obj is None:
        raise ValueError("not_found")
    # Аудит L1: organizer правит только свои посты; admin — любые.
    if obj.author_id != requester_id and not is_admin:
        raise ValueError("forbidden")

    data = dict(fields)
    if "content" in data and data["content"] is not None:
        data["content"] = sanitize_post_html(data["content"])
    # slug пересоздаём только при реальной смене title — иначе публичный
    # URL поста менялся бы при каждом сохранении (битые ссылки/SEO).
    new_title = data.get("title")
    if new_title and new_title != obj.title:
        data["slug"] = await _unique_slug(db, new_title, exclude_id=obj.id)

    for key, value in data.items():
        setattr(obj, key, value)

    await db.commit()
    reloaded = await repo.get_by_id(db, post_id)
    assert reloaded is not None  # invariant: только что обновили — точно есть
    return reloaded


async def delete_post(
    db: AsyncSession,
    post_id: uuid.UUID,
    *,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    obj = await repo.get_by_id(db, post_id)
    if obj is None:
        raise ValueError("not_found")
    # Аудит L1: organizer удаляет только свои посты; admin — любые.
    if obj.author_id != requester_id and not is_admin:
        raise ValueError("forbidden")
    await db.delete(obj)
    await db.commit()
