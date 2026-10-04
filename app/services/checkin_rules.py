"""
Доменные правила чек-ина (чистые функции, без БД).

Отдельно от сервиса по той же причине, что и show_rules.py: правила
допуска меняются, их удобно читать и тестировать в одном месте.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date

from app.models.dog import DogDocument, DogDocumentKind
from app.models.show import (
    AttendanceStatus,
    EntryCheckKind,
    EntryCheckResult,
    ShowStatus,
)
from app.services.show_rules import WORKING_CLASS_CODE

PRECHECK_STATUSES = frozenset({ShowStatus.registration_open, ShowStatus.registration_closed})
ONSITE_STATUSES = frozenset({ShowStatus.registration_closed, ShowStatus.in_progress})
TICKET_STATUSES = frozenset(
    {ShowStatus.registration_open, ShowStatus.registration_closed, ShowStatus.in_progress}
)

VET_PASSPORT_MISSING = "vet_passport_missing"
RABIES_DATE_MISSING = "rabies_date_missing"
RABIES_EXPIRED = "rabies_expired"
PEDIGREE_MISSING = "pedigree_missing"
WORKING_CERTIFICATE_MISSING = "working_certificate_missing"

PROBLEM_LABELS: dict[str, str] = {
    VET_PASSPORT_MISSING: "Не загружен ветпаспорт",
    RABIES_DATE_MISSING: "Не указан срок действия прививки от бешенства",
    RABIES_EXPIRED: "Прививка от бешенства истекает до окончания выставки",
    PEDIGREE_MISSING: "Не загружена родословная или метрика",
    WORKING_CERTIFICATE_MISSING: "Для рабочего класса нужен рабочий сертификат",
}




def reference_date(date_start: date, date_end: date | None) -> date:
    """Прививка должна действовать весь период выставки — сверяем по последнему дню."""
    return date_end or date_start


def allowed_statuses_for(kind: EntryCheckKind) -> frozenset[ShowStatus]:
    if kind == EntryCheckKind.docs_precheck:
        return PRECHECK_STATUSES
    return ONSITE_STATUSES


def current_documents(docs: Iterable[DogDocument]) -> dict[DogDocumentKind, DogDocument]:
    """Действующий документ каждого вида — последний загруженный."""
    result: dict[DogDocumentKind, DogDocument] = {}
    for doc in docs:
        cur = result.get(doc.kind)
        if cur is None or doc.created_at > cur.created_at:
            result[doc.kind] = doc
    return result


def rabies_valid_for(current: Mapping[DogDocumentKind, DogDocument], ref_date: date) -> bool | None:
    """None — оценить нельзя (нет ветпаспорта или даты)."""
    vp = current.get(DogDocumentKind.vet_passport)
    if vp is None or vp.valid_until is None:
        return None
    return vp.valid_until >= ref_date


def document_problems(
    current: Mapping[DogDocumentKind, DogDocument], ref_date: date, class_code: str | None
) -> list[str]:
    problems: list[str] = []
    vp = current.get(DogDocumentKind.vet_passport)
    if vp is None:
        problems.append(VET_PASSPORT_MISSING)
    elif vp.valid_until is None:
        problems.append(RABIES_DATE_MISSING)
    elif vp.valid_until < ref_date:
        problems.append(RABIES_EXPIRED)
    if DogDocumentKind.pedigree not in current and DogDocumentKind.puppy_card not in current:
        problems.append(PEDIGREE_MISSING)
    if class_code == WORKING_CLASS_CODE and DogDocumentKind.working_certificate not in current:
        problems.append(WORKING_CERTIFICATE_MISSING)
    return problems


def compute_attendance_status(
    latest: Mapping[EntryCheckKind, EntryCheckResult], current: AttendanceStatus
) -> AttendanceStatus:
    """
    Статус из последних отметок каждого вида:
    1. vet или docs_onsite провалены → rejected;
    2. arrival+vet+docs_onsite пройдены → admitted;
    3. arrival пройден → arrived;
    4. иначе — исходный статус (registered/absent). Если до этого был
       arrived/admitted/rejected, а отметки отменены исправлением —
       откатываемся в registered.
    docs_precheck на статус не влияет.
    """
    arrival = latest.get(EntryCheckKind.arrival)
    vet = latest.get(EntryCheckKind.vet)
    docs = latest.get(EntryCheckKind.docs_onsite)
    if EntryCheckResult.failed in (vet, docs):
        return AttendanceStatus.rejected
    if arrival == vet == docs == EntryCheckResult.passed:
        return AttendanceStatus.admitted
    if arrival == EntryCheckResult.passed:
        return AttendanceStatus.arrived
    if current in (AttendanceStatus.registered, AttendanceStatus.absent):
        return current
    return AttendanceStatus.registered
