"""
Pydantic-схемы блог-постов (этап 17).

Всё snake_case, без alias-генераторов и обёрток — бэкенд единообразен с
остальным API (DogResponse, NotificationResponse). Списки — пагинированная
обёртка PostPage той же формы, что DogPage (items/total/page/per_page);
detail — объект PostResponse напрямую, без {"post": …}. Перекладку в
camelCase и обёртки делает фронт в своём adapter-слое.

Сборка ответа (author/comments/favorite_person) вынесена в функции
to_card/to_response: эти поля — не прямые атрибуты ORM-поста, поэтому
model_validate(post) тут не годится, собираем явно. author резолвится из
предзагруженного post.author (+ profile), коллекции — пустые списки (никогда
не null), чтобы клиент не падал.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.post import Post, PostPublish
from app.utils.names import full_name


class Author(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    avatar_url: str = ""


class PostCard(BaseModel):
    """Карточка для списков/related — без тяжёлого content и meta_*."""

    id: uuid.UUID
    title: str
    slug: str
    description: str = ""
    cover_url: str | None = None
    created_at: datetime
    author: Author
    total_views: int = 0
    total_shares: int = 0
    total_comments: int = 0
    total_favorites: int = 0
    tags: list[str] = Field(default_factory=list)
    publish: PostPublish


class PostResponse(PostCard):
    """Полный пост: карточка + content, meta_*, v1-пустышки comments/favorite."""

    updated_at: datetime
    content: str = ""
    meta_title: str | None = None
    meta_description: str | None = None
    meta_keywords: list[str] = Field(default_factory=list)
    comments: list[Any] = Field(default_factory=list)
    favorite_person: list[Any] = Field(default_factory=list)


class PostCreate(BaseModel):
    title: str = Field(..., max_length=300)
    description: str = ""
    content: str = ""
    cover_url: str | None = Field(None, max_length=1024)
    tags: list[str] = Field(default_factory=list)
    meta_keywords: list[str] = Field(default_factory=list)
    meta_title: str | None = Field(None, max_length=300)
    meta_description: str | None = None
    publish: PostPublish = PostPublish.draft


class PostUpdate(BaseModel):
    title: str | None = Field(None, max_length=300)
    description: str | None = None
    content: str | None = None
    cover_url: str | None = Field(None, max_length=1024)
    tags: list[str] | None = None
    meta_keywords: list[str] | None = None
    meta_title: str | None = Field(None, max_length=300)
    meta_description: str | None = None
    publish: PostPublish | None = None


class PostPage(BaseModel):
    """Пагинированный список — один-в-один форма DogPage."""

    items: list[PostCard]
    total: int
    page: int
    per_page: int


def build_author(user: Any | None) -> Author:
    """Author из ORM-пользователя. None (author удалён) → пустой объект, не null.
    name — ФИО из профиля либо email; avatar — /files/{id} главного фото."""
    if user is None:
        return Author(name="", avatar_url="")
    avatar = f"/files/{user.avatar_file_id}" if user.avatar_file_id else ""
    return Author(name=full_name(user) or user.email, avatar_url=avatar)


def to_card(post: Post) -> PostCard:
    """ORM-пост → PostCard. Требует предзагруженного post.author (+ profile)."""
    return PostCard(
        id=post.id,
        title=post.title,
        slug=post.slug,
        description=post.description or "",
        cover_url=post.cover_url,
        created_at=post.created_at,
        author=build_author(post.author),
        total_views=post.total_views,
        total_shares=post.total_shares,
        total_comments=post.total_comments,
        total_favorites=post.total_favorites,
        tags=list(post.tags or []),
        publish=post.publish,
    )


def to_response(post: Post) -> PostResponse:
    """ORM-пост → полный PostResponse (объект напрямую, без обёртки)."""
    return PostResponse(
        id=post.id,
        title=post.title,
        slug=post.slug,
        description=post.description or "",
        cover_url=post.cover_url,
        created_at=post.created_at,
        updated_at=post.updated_at,
        author=build_author(post.author),
        total_views=post.total_views,
        total_shares=post.total_shares,
        total_comments=post.total_comments,
        total_favorites=post.total_favorites,
        tags=list(post.tags or []),
        publish=post.publish,
        content=post.content or "",
        meta_title=post.meta_title,
        meta_description=post.meta_description,
        meta_keywords=list(post.meta_keywords or []),
        comments=[],
        favorite_person=[],
    )
