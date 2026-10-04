"""
Сервис чек-ина: флаг выставки, персонал, билет участника, стойка
(скан/поиск/отметки), сводка и очередь предпроверки.

Ошибки — ValueError("code"), маппинг в HTTP — routers/checkin.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import DogDocumentKind
from app.models.show import (
    AttendanceStatus,
    EntryCheck,
    EntryCheckKind,
    Show,
    ShowEntry,
    ShowStaff,
    ShowStaffRole,
    ShowStatus,
)
from app.models.user import User
from app.repositories import checkin as repo
from app.repositories import show as show_repo
from app.repositories import user as user_repo
from app.schemas.checkin import (
    CardDog,
    CheckinSummary,
    EntryCard,
    EntryCheckCreate,
    EntryCheckResponse,
    ParticipantCard,
    ShowStaffResponse,
    TicketEntry,
    TicketResponse,
)
from app.services import checkin_rules as rules
from app.services import dog_document
from app.utils import checkin_token
from app.utils.names import full_name


def display_name(user: User | None) -> str:
    """ФИО → email → телефон: на стойке нужно хоть как-то назвать человека."""
    if user is None:
        return "—"
    return full_name(user) or user.phone or "—"


async def get_show(db: AsyncSession, show_id: uuid.UUID) -> Show:
    show = await show_repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    return show


def ensure_organizer(show: Show, user_id: uuid.UUID, is_admin: bool) -> None:
    if not is_admin and show.organizer_id != user_id:
        raise ValueError("forbidden")


async def ensure_desk_access(
    db: AsyncSession, show: Show, user_id: uuid.UUID, is_admin: bool
) -> None:
    if is_admin or show.organizer_id == user_id:
        return
    if await repo.is_staff(db, show.id, user_id):
        return
    raise ValueError("forbidden")


def ensure_enabled(show: Show) -> None:
    if not show.checkin_enabled:
        raise ValueError("checkin_disabled")


async def set_enabled(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool, enabled: bool
) -> Show:
    show = await get_show(db, show_id)
    ensure_organizer(show, user_id, is_admin)
    if show.status in (ShowStatus.completed, ShowStatus.cancelled):
        raise ValueError("show_locked")
    show.checkin_enabled = enabled
    await db.commit()
    await db.refresh(show)
    return show


def _staff_response(staff: ShowStaff, user: User) -> ShowStaffResponse:
    return ShowStaffResponse(
        user_id=user.id, role=staff.role, display_name=display_name(user),
        email=user.email, phone=user.phone, created_at=staff.created_at,
    )


async def list_staff(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID, is_admin: bool
) -> list[ShowStaffResponse]:
    show = await get_show(db, show_id)
    ensure_organizer(show, user_id, is_admin)
    return [_staff_response(s, u) for s, u in await repo.list_staff(db, show_id)]


async def add_staff(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    *,
    email: str | None,
    phone: str | None,
) -> ShowStaffResponse:
    show = await get_show(db, show_id)
    ensure_organizer(show, requester_id, is_admin)
    if email is not None:
        user = await repo.get_user_by_email_ci(db, email)
    else:
        user = await user_repo.get_user_by_phone(db, phone or "")
    if user is None:
        raise ValueError("user_not_found")
    # Явная проверка — обычный путь; IntegrityError ниже — страховка от
    # гонки двух одновременных добавлений.
    if await repo.is_staff(db, show.id, user.id):
        raise ValueError("already_staff")
    staff = ShowStaff(
        show_id=show.id, user_id=user.id, role=ShowStaffRole.registrar,
        added_by=requester_id,
    )
    db.add(staff)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("already_staff") from None
    await db.refresh(staff)
    for s, u in await repo.list_staff(db, show.id):
        if s.id == staff.id:
            return _staff_response(s, u)
    raise ValueError("not_found")  # недостижимо: строку только что вставили


async def remove_staff(
    db: AsyncSession, show_id: uuid.UUID, requester_id: uuid.UUID, is_admin: bool,
    user_id: uuid.UUID,
) -> None:
    show = await get_show(db, show_id)
    ensure_organizer(show, requester_id, is_admin)
    if await repo.delete_staff(db, show_id, user_id) == 0:
        raise ValueError("staff_not_found")
    await db.commit()


async def list_my_shows(db: AsyncSession, user_id: uuid.UUID) -> list[Show]:
    return await repo.list_staffed_shows(db, user_id)


def _check_response(c: EntryCheck, users: dict[uuid.UUID, User]) -> EntryCheckResponse:
    performer = users.get(c.performed_by) if c.performed_by else None
    return EntryCheckResponse(
        id=c.id, kind=c.kind, result=c.result, comment=c.comment,
        document_id=c.document_id, performed_by=c.performed_by,
        performed_by_name=display_name(performer) if performer else None,
        created_at=c.created_at,
    )


async def build_entry_cards(
    db: AsyncSession, show: Show, entries: list[ShowEntry]
) -> list[EntryCard]:
    if not entries:
        return []
    dogs, classes, users, avatars, doc_rows, checks = await repo.load_card_context(db, entries)
    performers = await repo.load_users(db, [c.performed_by for c in checks if c.performed_by])
    ref = rules.reference_date(show.date_start, show.date_end)
    cards: list[EntryCard] = []
    for e in entries:
        dog = dogs[e.dog_id]
        cls = classes[e.show_class_id]
        dog_rows = [(d, f) for d, f in doc_rows if d.dog_id == dog.id]
        current = rules.current_documents([d for d, _ in dog_rows])
        latest: dict[EntryCheckKind, EntryCheckResponse] = {}
        for c in checks:  # отсортированы по времени — последняя перезаписывает
            if c.entry_id == e.id:
                latest[c.kind] = _check_response(c, performers)
        # Предпроверка относится к сканам, загруженным ДО неё: после нового
        # документа старая отметка (особенно «одобрено») вводила бы стойку
        # в заблуждение.
        precheck = latest.get(EntryCheckKind.docs_precheck)
        if precheck and dog_rows and max(d.created_at for d, _ in dog_rows) > precheck.created_at:
            del latest[EntryCheckKind.docs_precheck]
        participant = users.get(e.registered_by)
        vp = current.get(DogDocumentKind.vet_passport)
        cards.append(EntryCard(
            entry_id=e.id, show_id=e.show_id, catalog_number=e.catalog_number,
            attendance_status=e.attendance_status,
            class_code=cls.code, class_name=cls.name,
            dog=CardDog(
                id=dog.id, name=dog.name, microchip=dog.microchip, tattoo=dog.tattoo,
                rkf_number=dog.rkf_number, avatar_file_id=avatars.get(dog.id),
            ),
            participant_id=e.registered_by,
            participant_name=display_name(participant),
            participant_phone=participant.phone if participant else None,
            documents=[r for r in dog_document.to_responses(dog_rows) if r.is_current],
            rabies_valid_until=vp.valid_until if vp else None,
            rabies_valid_for_show=rules.rabies_valid_for(current, ref),
            problems=rules.document_problems(current, ref, cls.code),
            latest_checks=latest,
        ))
    return cards


async def get_ticket(db: AsyncSession, show_id: uuid.UUID, user: User) -> TicketResponse:
    show = await get_show(db, show_id)
    ensure_enabled(show)
    if show.status not in rules.TICKET_STATUSES:
        raise ValueError("invalid_show_status")
    entries = await repo.list_participant_entries(db, show.id, user.id)
    if not entries:
        raise ValueError("no_entries")
    cards = await build_entry_cards(db, show, entries)
    return TicketResponse(
        show_id=show.id,
        token=checkin_token.make_token(show.id, user.id),
        entries=[
            TicketEntry(
                entry_id=c.entry_id, dog_id=c.dog.id, dog_name=c.dog.name,
                class_name=c.class_name, catalog_number=c.catalog_number,
                attendance_status=c.attendance_status, problems=c.problems,
            )
            for c in cards
        ],
    )


async def _desk_show(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool, *, onsite: bool
) -> Show:
    show = await get_show(db, show_id)
    await ensure_desk_access(db, show, user.id, is_admin)
    ensure_enabled(show)
    if onsite and show.status not in rules.ONSITE_STATUSES:
        raise ValueError("invalid_show_status")
    return show


async def scan(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool, token: str
) -> ParticipantCard:
    show = await _desk_show(db, show_id, user, is_admin, onsite=True)
    try:
        token_show_id, participant_id = checkin_token.parse_token(token)
    except checkin_token.InvalidCheckinToken:
        raise ValueError("invalid_token") from None
    if token_show_id != show.id:
        raise ValueError("token_other_show")
    entries = await repo.list_participant_entries(db, show.id, participant_id)
    if not entries:
        raise ValueError("no_entries")
    participant = (await repo.load_users(db, [participant_id])).get(participant_id)
    return ParticipantCard(
        user_id=participant_id,
        display_name=display_name(participant),
        phone=participant.phone if participant else None,
        email=participant.email if participant else None,
        entries=await build_entry_cards(db, show, entries),
    )


async def search(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool, q: str
) -> list[EntryCard]:
    show = await _desk_show(db, show_id, user, is_admin, onsite=True)
    if len(q.strip()) < 2 and not q.strip().isdigit():
        return []
    return await build_entry_cards(db, show, await repo.search_entries(db, show.id, q))


async def add_checks(
    db: AsyncSession,
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    user: User,
    is_admin: bool,
    checks: list[EntryCheckCreate],
) -> EntryCard:
    show = await _desk_show(db, show_id, user, is_admin, onsite=False)
    for c in checks:
        if show.status not in rules.allowed_statuses_for(c.kind):
            raise ValueError("invalid_show_status")
    # FOR UPDATE: две стойки на одну собаку сериализуются, статус
    # пересчитывается по полной истории, а не по «своей» половине.
    entry = await repo.get_entry_for_update(db, show.id, entry_id)
    if entry is None:
        raise ValueError("entry_not_found")
    doc_ids = {c.document_id for c in checks if c.document_id}
    if doc_ids:
        own = {d.id for d, _ in await repo.list_dog_documents(db, [entry.dog_id])}
        if not doc_ids <= own:
            raise ValueError("document_mismatch")
    for c in checks:
        db.add(EntryCheck(
            entry_id=entry.id, kind=c.kind, result=c.result,
            comment=(c.comment or "").strip() or None,
            document_id=c.document_id, performed_by=user.id,
        ))
    await db.flush()
    latest = {c.kind: c.result for c in await repo.list_checks(db, [entry.id])}
    new_status = rules.compute_attendance_status(latest, entry.attendance_status)
    if new_status != entry.attendance_status:
        entry.attendance_status = new_status
        entry.attendance_changed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(entry)
    return (await build_entry_cards(db, show, [entry]))[0]


async def list_entry_checks(
    db: AsyncSession, show_id: uuid.UUID, entry_id: uuid.UUID, user: User, is_admin: bool
) -> list[EntryCheckResponse]:
    show = await get_show(db, show_id)
    await ensure_desk_access(db, show, user.id, is_admin)
    entry = await show_repo.get_show_entry(db, entry_id)
    if entry is None or entry.show_id != show.id:
        raise ValueError("entry_not_found")
    checks = await repo.list_checks(db, [entry.id])
    performers = await repo.load_users(db, [c.performed_by for c in checks if c.performed_by])
    return [_check_response(c, performers) for c in reversed(checks)]


async def summary(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool
) -> CheckinSummary:
    show = await get_show(db, show_id)
    await ensure_desk_access(db, show, user.id, is_admin)
    counts = await repo.attendance_counts(db, show.id)
    return CheckinSummary(
        total=sum(counts.values()),
        **{s.value: counts.get(s, 0) for s in AttendanceStatus},
    )


async def precheck_queue(
    db: AsyncSession, show_id: uuid.UUID, user: User, is_admin: bool
) -> list[EntryCard]:
    show = await get_show(db, show_id)
    await ensure_desk_access(db, show, user.id, is_admin)
    ensure_enabled(show)
    return await build_entry_cards(db, show, await repo.precheck_queue(db, show.id))
