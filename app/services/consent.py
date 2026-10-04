"""
Согласия пользователя на обработку персональных данных (152-ФЗ).

Виды:
- terms — принятие Пользовательского соглашения (оферта, ст. 438 ГК РФ);
- personal_data — согласие на обработку ПДн (ст. 9 152-ФЗ). С 01.09.2025
  оформляется отдельно от иных документов (ред. 156-ФЗ), поэтому это
  отдельная отметка, а не часть terms;
- public_kennel_contacts / public_classified_contacts — согласие на
  распространение (ст. 10.1 152-ФЗ). Даётся по конкретной публикации
  (target_id) переключателем contacts_public, отдельно от остальных.

CURRENT_REVISIONS синхронизировать с LEGAL_REVISIONS во фронтенде
(src/sections/legal/operator.ts): при новой редакции документа старые
согласия перестают считаться актуальными, и интерфейс попросит
подтвердить заново (missing_required).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consent import UserConsent


class ConsentKind(str, enum.Enum):
    terms = "terms"
    personal_data = "personal_data"
    public_kennel_contacts = "public_kennel_contacts"
    public_classified_contacts = "public_classified_contacts"


# Даются на уровне аккаунта; без них аккаунт не создаётся.
ACCOUNT_KINDS: tuple[ConsentKind, ...] = (
    ConsentKind.terms,
    ConsentKind.personal_data,
)

CURRENT_REVISIONS: dict[ConsentKind, str] = {
    ConsentKind.terms: "2026-10-04",
    ConsentKind.personal_data: "2026-10-04",
    ConsentKind.public_kennel_contacts: "2026-10-04",
    ConsentKind.public_classified_contacts: "2026-10-04",
}


class ConsentRequiredError(Exception):
    """Новый аккаунт без обязательных согласий."""


async def _active(
    db: AsyncSession,
    user_id: uuid.UUID,
    kind: ConsentKind,
    target_id: uuid.UUID | None,
) -> list[UserConsent]:
    stmt = select(UserConsent).where(
        UserConsent.user_id == user_id,
        UserConsent.kind == kind.value,
        UserConsent.revoked_at.is_(None),
    )
    if target_id is None:
        stmt = stmt.where(UserConsent.target_id.is_(None))
    else:
        stmt = stmt.where(UserConsent.target_id == target_id)
    return list((await db.execute(stmt)).scalars().all())


async def grant(
    db: AsyncSession,
    user_id: uuid.UUID,
    kind: ConsentKind,
    *,
    target_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """
    Записать согласие на текущую редакцию. Идемпотентно: если актуальное
    согласие той же редакции уже есть — ничего не пишем (повторный вход
    по телефону с отметкой не плодит строк). Не коммитит.
    """
    revision = CURRENT_REVISIONS[kind]
    active = await _active(db, user_id, kind, target_id)
    if any(c.revision == revision for c in active):
        return
    db.add(
        UserConsent(
            user_id=user_id,
            kind=kind.value,
            revision=revision,
            target_id=target_id,
            ip=ip,
            user_agent=(user_agent or "")[:512] or None,
        )
    )
    await db.flush()


async def revoke(
    db: AsyncSession,
    user_id: uuid.UUID,
    kind: ConsentKind,
    *,
    target_id: uuid.UUID | None = None,
) -> None:
    """Отозвать все действующие согласия вида. Не коммитит."""
    stmt = (
        update(UserConsent)
        .where(
            UserConsent.user_id == user_id,
            UserConsent.kind == kind.value,
            UserConsent.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    if target_id is None:
        stmt = stmt.where(UserConsent.target_id.is_(None))
    else:
        stmt = stmt.where(UserConsent.target_id == target_id)
    await db.execute(stmt)


async def list_active(
    db: AsyncSession, user_id: uuid.UUID
) -> list[UserConsent]:
    stmt = (
        select(UserConsent)
        .where(
            UserConsent.user_id == user_id,
            UserConsent.revoked_at.is_(None),
        )
        .order_by(UserConsent.granted_at)
    )
    return list((await db.execute(stmt)).scalars().all())


def missing_required(active: list[UserConsent]) -> list[str]:
    """Обязательные согласия, которых нет в актуальной редакции."""
    have = {
        c.kind
        for c in active
        if c.target_id is None
        and c.revision == CURRENT_REVISIONS.get(ConsentKind(c.kind))
    }
    return [k.value for k in ACCOUNT_KINDS if k.value not in have]


async def set_publication_consent(
    db: AsyncSession,
    user_id: uuid.UUID,
    kind: ConsentKind,
    target_id: uuid.UUID,
    enabled: bool,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Синхронизировать журнал с переключателем contacts_public."""
    if enabled:
        await grant(
            db, user_id, kind, target_id=target_id, ip=ip, user_agent=user_agent
        )
    else:
        await revoke(db, user_id, kind, target_id=target_id)
