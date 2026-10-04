"""
Удаление аккаунта пользователем (ст. 14, 21 152-ФЗ; п. 16.2–16.3
Пользовательского соглашения).

Строку users физически не удаляем: на неё ссылаются выставки, записи,
судейство и рекламные кампании (FK RESTRICT — историчность мероприятий
и биллинга). Вместо этого аккаунт обезличивается:

- users: телефон, email, пароль, аватар → удалены; email заменён на
  несуществующий адрес в зоне .invalid (RFC 2606), т.к. CHECK требует
  email или phone; is_active=False, deleted_at=now;
- удаляются: профиль (ФИО, соцсети), роли, сессии, токены, согласия,
  объявления, обращения в поддержку, подписки и уведомления, сканы
  документов собак (ветпаспорта и т.п. содержат ПДн);
- питомник остаётся (на него ссылаются собаки и родословные), но без
  контактов и сайта;
- собаки остаются как историческая запись (результаты, родословные),
  но отвязываются от человека (owner_id = NULL).

Журнал безопасности не трогаем: он хранится ограниченный срок по
законному интересу (расследование угонов) и чистится по расписанию.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ad import AdCampaign, CampaignStatus
from app.models.classified import Classified
from app.models.consent import UserConsent
from app.models.dog import Dog, DogDocument
from app.models.file import UploadedFile
from app.models.kennel import Kennel
from app.models.notification import Notification, Subscription
from app.models.show import Show, ShowStatus
from app.models.support import SupportTicket
from app.models.user import (
    EmailVerificationToken,
    RefreshToken,
    User,
    UserProfile,
    UserRole,
)
from app.repositories import security_audit as audit_repo
from app.services import file_storage
from app.services.account_security import _consume_or_400
from app.services.otp_auth import OTPPurpose
from app.utils.security import dummy_verify_password, verify_password

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("app.security")

# Выставки в этих статусах ещё не состоялись: удаление организатора
# оставило бы участников без контакта с ним.
_UNFINISHED_SHOW_STATUSES = (
    ShowStatus.draft,
    ShowStatus.registration_open,
    ShowStatus.registration_closed,
    ShowStatus.in_progress,
)


async def _reauth(
    redis: Redis, user: User, code: str | None, password: str | None
) -> None:
    """Подтверждение личности: код на номер аккаунта или пароль."""
    if user.phone and user.is_phone_verified:
        if not code:
            raise HTTPException(status_code=400, detail="code_required")
        await _consume_or_400(
            redis, code, purpose=OTPPurpose.reauth, subject=str(user.id)
        )
        return
    if not password or not user.hashed_password:
        dummy_verify_password()
        raise HTTPException(status_code=403, detail="invalid_password")
    if not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=403, detail="invalid_password")


async def _ensure_no_blockers(db: AsyncSession, user: User) -> None:
    shows = await db.execute(
        select(Show.id).where(
            Show.organizer_id == user.id,
            Show.status.in_(_UNFINISHED_SHOW_STATUSES),
            Show.date_start >= date.today(),
        ).limit(1)
    )
    if shows.first() is not None:
        raise HTTPException(status_code=409, detail="active_shows")
    campaigns = await db.execute(
        select(AdCampaign.id).where(
            AdCampaign.advertiser_id == user.id,
            AdCampaign.status == CampaignStatus.active,
        ).limit(1)
    )
    if campaigns.first() is not None:
        raise HTTPException(status_code=409, detail="active_ad_campaigns")


async def delete_account(
    db: AsyncSession,
    redis: Redis,
    user: User,
    *,
    code: str | None,
    password: str | None,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Обезличить аккаунт. Коммитит сам; файлы из S3 удаляет после commit."""
    # Блокеры — до сжигания кода: иначе пользователь тратил бы код на
    # заведомо невозможное действие.
    await _ensure_no_blockers(db, user)
    await _reauth(redis, user, code, password)

    uid = user.id

    # --- Сканы документов собак пользователя -------------------------
    doc_files = (
        await db.execute(
            select(UploadedFile.id, UploadedFile.s3_key)
            .join(DogDocument, DogDocument.file_id == UploadedFile.id)
            .join(Dog, Dog.id == DogDocument.dog_id)
            .where(Dog.owner_id == uid)
        )
    ).all()
    file_ids = [row.id for row in doc_files]
    s3_keys = [row.s3_key for row in doc_files]
    if user.avatar_file_id is not None:
        avatar = await db.get(UploadedFile, user.avatar_file_id)
        if avatar is not None:
            file_ids.append(avatar.id)
            s3_keys.append(avatar.s3_key)

    await db.execute(
        delete(DogDocument).where(
            DogDocument.dog_id.in_(select(Dog.id).where(Dog.owner_id == uid))
        )
    )

    # --- Контент и служебные данные пользователя ----------------------
    for model, column in (
        (Classified, Classified.author_id),
        (SupportTicket, SupportTicket.user_id),
        (Subscription, Subscription.user_id),
        (Notification, Notification.user_id),
        (UserConsent, UserConsent.user_id),
        (RefreshToken, RefreshToken.user_id),
        (EmailVerificationToken, EmailVerificationToken.user_id),
        (UserRole, UserRole.user_id),
        (UserProfile, UserProfile.user_id),
    ):
        await db.execute(delete(model).where(column == uid))

    await db.execute(
        update(Kennel)
        .where(Kennel.owner_id == uid)
        .values(
            contact_phone=None,
            contact_email=None,
            website=None,
            contacts_public=False,
        )
    )
    await db.execute(update(Dog).where(Dog.owner_id == uid).values(owner_id=None))

    # --- Сама учётная запись -----------------------------------------
    user.avatar_file_id = None
    await db.flush()
    if file_ids:
        await db.execute(delete(UploadedFile).where(UploadedFile.id.in_(file_ids)))

    user.phone = None
    user.email = f"deleted-{uid.hex}@deleted.invalid"
    user.pending_email = None
    user.hashed_password = None
    user.is_email_verified = False
    user.is_phone_verified = False
    user.is_active = False
    user.deleted_at = datetime.now(timezone.utc)

    await audit_repo.record_security_event(
        db,
        user_id=uid,
        action="account_deleted",
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    security_logger.info("account_deleted user_id=%s", uid)

    # Best-effort: запись в БД уже удалена, висячий объект в S3 без ссылки
    # недоступен через API. file_storage.delete_file сам глушит ошибки.
    for key in s3_keys:
        await file_storage.delete_file(key)
