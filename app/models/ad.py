"""
Модели рекламного модуля (этап 10).

Структура:
- AdCampaign — контейнер: рекламодатель, бюджет, период, статус.
- AdBanner — конкретный баннер кампании с таргетингом.
- AdEvent — событие (impression/click), сырые данные для аналитики.

Решения:
- spent в БД (а не считать на лету через ad_events) — на горячем запросе
  /ads/serve нужно проверить "spent < budget" быстро. Денормализация ради
  скорости; источник истины (события) остаётся в ad_events.
- Атомарное списание бюджета: UPDATE SET spent=spent+:cost WHERE
  spent+:cost <= budget — реализовано в репозитории.
- impressions_count / clicks_count на баннере — тоже денормализация
  ради дашборда; пересчёт можно делать cron'ом из ad_events.
- ad_events: на проде должен быть PARTITIONED BY RANGE (created_at).
  В Alembic-миграции этого этапа партиционирование не вводим:
  - dev-объёмы крошечные, индекса достаточно,
  - перевод существующей таблицы на partitioned требует пересоздания —
    лучше делать одной production-миграцией на этапе 14.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CampaignStatus(str, enum.Enum):
    draft = "draft"          # ещё не запущена
    active = "active"        # активна и показывается
    paused = "paused"        # приостановлена рекламодателем
    completed = "completed"  # бюджет израсходован / период истёк
    cancelled = "cancelled"  # отменена


class BannerPlacement(str, enum.Enum):
    """
    Места размещения баннера. Enum жёсткий: фронту нужно знать, какие
    placements есть, чтобы не запрашивать неизвестные. Новое место
    добавляется миграцией (alembic upgrade).
    """

    sidebar = "sidebar"          # боковая колонка
    top = "top"                  # шапка страницы
    inline = "inline"            # в ленте/каталоге между карточками
    footer = "footer"            # подвал


class AdEventType(str, enum.Enum):
    impression = "impression"
    click = "click"


class AdCampaign(Base, TimestampMixin):
    __tablename__ = "ad_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    advertiser_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT — нельзя удалить юзера-рекламодателя, пока есть его
        # кампании. Биллинг должен быть корректным даже после ухода.
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Numeric для денег — никаких float'ов. 12 знаков покрывают любые
    # реальные бюджеты в рублях.
    budget: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # spent — денормализация ради скорости /ads/serve (см. модульный
    # докстринг). Должна обновляться атомарно вместе с записью события.
    spent: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), server_default="0"
    )
    # Cost-per-impression: фиксированная цена за показ. CPC (за клик)
    # — поле под расширение на следующем этапе.
    cost_per_impression: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("0.01"), server_default="0.01"
    )

    date_start: Mapped[date] = mapped_column(Date, index=True)
    date_end: Mapped[date] = mapped_column(Date, index=True)

    status: Mapped[CampaignStatus] = mapped_column(
        SAEnum(CampaignStatus, name="campaignstatus"),
        default=CampaignStatus.draft,
        index=True,
    )

    banners: Mapped[list["AdBanner"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
    )


class AdBanner(Base, TimestampMixin):
    __tablename__ = "ad_banners"
    __table_args__ = (
        # Композитный индекс под основной запрос /ads/serve:
        # "активные баннеры в placement". Без индекса с ростом таблицы
        # деградирует horror-fast.
        Index("ix_ad_banners_serve", "placement", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ad_campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    # FK на UploadedFile (этап 4). image сначала грузится через
    # POST /files/upload, потом file_id привязывается сюда.
    image_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Куда вести при клике. Не EmailStr/HttpUrl, потому что:
    # - HttpUrl при model_dump(mode="json") меняет тип строки;
    # - проще валидировать regex'ом в Pydantic-схеме.
    target_url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    placement: Mapped[BannerPlacement] = mapped_column(
        SAEnum(BannerPlacement, name="bannerplacement"),
        index=True,
    )

    # --- Таргетинг (NULL = "любой") ---
    target_animal_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("animal_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_breed_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("breeds.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_region: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )

    # Денормализованные счётчики ради скорости дашборда. Source of truth
    # — таблица ad_events.
    impressions_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    clicks_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )

    campaign: Mapped["AdCampaign"] = relationship(back_populates="banners")


class AdEvent(Base):
    """
    Сырое событие показа/клика. Хранится отдельно от агрегатов, чтобы:
    - не блокировать INSERT при чтениях дашборда,
    - можно было пересчитать счётчики при инциденте.

    Дедуп-поля (ip + user_agent_hash) — оставлены явно для post-mortem
    анализа фрода. В горячем пути дедупликация делается через Redis
    SET с TTL=60s (см. app/services/ad.py).

    TODO (этап 14, production-readiness):
    Партиционирование по диапазону created_at (помесячно). При
    миллионах строк это критично для производительности; на этапе 10
    оставляем плоской таблицей, чтобы alembic-миграция не была
    громоздкой.
    """

    __tablename__ = "ad_events"
    __table_args__ = (
        # Индекс под GROUP BY date(created_at) — статистика по дням.
        Index("ix_ad_events_banner_created", "banner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    banner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — события без баннера бессмысленны.
        ForeignKey("ad_banners.id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[AdEventType] = mapped_column(
        SAEnum(AdEventType, name="adeventtype"),
        index=True,
    )
    # Кто кликнул — опционально (анонимы тоже кликают баннеры).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # SHA-256(user_agent) — хранение хеша, а не сырой строки, экономит
    # место и анонимизирует данные. На длину 128 — с запасом.
    user_agent_hash: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    page_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
