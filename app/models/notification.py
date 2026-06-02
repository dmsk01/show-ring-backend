"""
Модели подписок и уведомлений (этап 9).

Две таблицы:

1. `subscriptions` — что пользователь хочет узнавать.
   Фильтры по породе и региону: NULL = "все породы / все регионы".
   Каналы доставки — пока только email; push добавится позднее с
   фронтовой web-push.

2. `notifications` — лог отправленных уведомлений.
   Хранится не сам текст письма (это можно реконструировать из event),
   а только метаданные: subject, статус доставки, ошибка. Хранение тел
   писем будет нужно только если потребуется "showuser the email they
   got" — пока этого нет в требованиях.

Архитектурное решение: события платформы публикуются в Topic Exchange
(см. app.services.notification). Воркер подписан на pattern (например,
"litter.announced.breed.*"), читает event, ищет подписчиков в
subscriptions, формирует email-задачу и публикует её в Task Queue.

Так почему именно два уровня (event → email_task)?
- Event описывает "что произошло" (litter создан), а не "кому
  отправить". Подписчиков знает воркер событий, не публикатор.
- Email_task — атомарная единица доставки. Если у нас 100 подписчиков,
  получится 100 email_task'ов в очереди, каждый retry'ится отдельно.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class NotificationStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class NotificationChannel(str, enum.Enum):
    email = "email"
    push = "push"  # резерв, пока не реализован


class EventType(str, enum.Enum):
    """
    Возможные события для подписки. Перечислены здесь как Enum,
    а в БД хранятся строки — чтобы добавление нового события не
    требовало миграции.

    Routing keys для топик-эксчейнджа формируются как:
    - "show.registration_opened"
    - "show.results_published"
    - "litter.announced.breed.<breed_id>"
    - "dog.title_earned"
    """

    SHOW_REGISTRATION_OPENED = "show.registration_opened"
    SHOW_RESULTS_PUBLISHED = "show.results_published"
    LITTER_ANNOUNCED = "litter.announced"
    DOG_TITLE_EARNED = "dog.title_earned"


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"
    __table_args__ = (
        # Не плодить дубликаты "та же подписка": одна подписка =
        # (user, event, filter_breed, filter_region, channel).
        # NULL в UniqueConstraint в PG считается отличным от NULL,
        # поэтому UNIQUE здесь — мягкая защита: совсем без фильтров
        # запись будет одна, с фильтрами могут быть варианты.
        UniqueConstraint(
            "user_id",
            "event_type",
            "filter_breed_id",
            "filter_region",
            "channel",
            name="uq_subscription_uniq",
        ),
        # Композитный индекс под "найти подписчиков на event_type
        # с породой X". Закрывает основной запрос воркера событий.
        Index(
            "ix_subscriptions_event_breed",
            "event_type",
            "filter_breed_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE — подписки умирают вместе с юзером (а не висят как
        # сироты). Это пользовательский контент, не исторические данные.
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    # event_type как строка, а не Enum в БД — новые типы событий
    # добавляются без миграции. На вход через Pydantic-схему проверим
    # принадлежность к EventType-перечислению.
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    filter_breed_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # SET NULL — если порода удалена из справочника, подписка
        # становится "на все породы" (а не пропадает совсем).
        ForeignKey("breeds.id", ondelete="SET NULL"),
        nullable=True,
    )
    filter_region: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        SAEnum(NotificationChannel, name="notificationchannel"),
        default=NotificationChannel.email,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )


class Notification(Base):
    """
    Лог уведомления, отправленного пользователю.

    Зачем хранить:
    - GET /notifications — пользователь видит историю.
    - debug проблем доставки: если письмо не пришло, в notifications
      виден status=failed с error.
    - аналитика: какие события генерируют отписки.
    """

    __tablename__ = "notifications"

    __table_args__ = (
        # bug_230 audit 2026-05-28: UNIQUE на message_id — защита от
        # двойной обработки одного события. events_handler формирует
        # message_id = uuid5(event_id, user_id), при повторной доставке
        # RabbitMQ INSERT упадёт с IntegrityError → пропускаем как
        # «уже отправлено».
        UniqueConstraint("message_id", name="uq_notifications_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # message_id — детерминированный per-recipient идемпотентный ключ.
    # nullable=True для исторических строк, созданных до миграции; всё
    # новое заполняется в events_handler.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    # event_type, как и в subscriptions — строка.
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[NotificationChannel] = mapped_column(
        SAEnum(NotificationChannel, name="notificationchannel"),
    )
    subject: Mapped[str] = mapped_column(String(300))
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, name="notificationstatus"),
        default=NotificationStatus.pending,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # read_at — когда пользователь прочитал уведомление в интерфейсе.
    # ОТДЕЛЬНО от status: status — это про доставку email
    # (pending/sent/failed), а read_at — про действие пользователя в UI.
    # NULL = непрочитано; в API отдаём is_read = (read_at IS NOT NULL),
    # чтобы фронт не выводил «прочитано» из delivery-статуса.
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # created_at без updated_at: уведомление "родилось" один раз,
    # дальше только статус и sent_at меняются — не хочу шумить апдейтом
    # неважного timestamp. server_default=now() — БД сама проставит при
    # INSERT, не полагаясь на client clock.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
