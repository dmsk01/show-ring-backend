"""
Модель блог-поста (этап 17).

Блог — публичная витрина статей/новостей платформы. Контракт фронта
(Minimal Kit) просил camelCase и обёртки, но проект единообразен: пост
отдаётся в snake_case и в тех же формах, что Dog/Notification, а перекладку
делает фронт у себя (см. docs/plans/stages/stage-17-blog.md).

Решения:
- slug UNIQUE + index — «естественный ключ» для публичных URL и lookup'а
  (как rkf_number у собаки, но обязательный и генерируемый из title).
- tags/meta_keywords как PG ARRAY(String) — одна колонка-массив вместо
  join-таблицы: проще и хватает под контракт (поиск тега = `tag = ANY(tags)`).
- publish как PG-enum (postpublish): строго published/draft, по умолчанию
  draft — черновик не виден в публичной выдаче, пока автор не опубликует.
- author_id FK → users SET NULL: при удалении пользователя пост остаётся
  историей, но без автора (тогда author резолвится в пустой объект, не null).
- total_* — денормализованные счётчики (просмотры/репосты/комментарии/
  избранное) колонками с дефолтом 0. Реальные комментарии/избранное —
  технический долг этапа; пока счётчики просто хранятся и отдаются.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class PostPublish(str, enum.Enum):
    published = "published"
    draft = "draft"


class Post(Base, TimestampMixin):
    __tablename__ = "posts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(300))
    # slug — публичный URL и ключ lookup'а. UNIQUE страхует от гонок при
    # параллельном создании постов с одинаковым title (сервис ещё и
    # подбирает суффикс -2, см. app/utils/slug.py).
    slug: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    description: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    # content — санитизированный HTML (allowlist-bleach в сервисе). Глобальный
    # SanitizationMiddleware это поле НЕ трогает (passthrough), иначе вырезал
    # бы весь HTML ещё до хендлера.
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Готовый URL обложки от фронта (он грузит файл через POST /files/upload
    # и кладёт сюда /files/{id}). Своего FK не держим — это просто строка.
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String), server_default="{}", default=list
    )
    meta_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(String), server_default="{}", default=list
    )
    meta_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish: Mapped[PostPublish] = mapped_column(
        SAEnum(PostPublish, name="postpublish"),
        default=PostPublish.draft,
        server_default="draft",
        index=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    total_views: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    total_shares: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    total_comments: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    total_favorites: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )

    # Одностороннее отношение к автору (у User нет обратного posts — блог
    # вторичен к модели пользователя). Грузим только через selectinload в
    # репозитории (вместе с author.profile для ФИО), чтобы не словить N+1.
    author: Mapped["User | None"] = relationship("User")  # noqa: F821
