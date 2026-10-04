# tests/unit/test_checkin_rules.py
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from app.models.dog import DogDocumentKind as K
from app.models.show import AttendanceStatus as A
from app.models.show import EntryCheckKind as C
from app.models.show import EntryCheckResult as R
from app.services import checkin_rules as rules


@dataclass
class Doc:
    kind: K
    created_at: datetime
    valid_until: date | None = None


T0 = datetime(2026, 1, 1)
REF = date(2026, 10, 10)


def test_reference_date_prefers_end():
    assert rules.reference_date(date(2026, 1, 1), date(2026, 1, 2)) == date(2026, 1, 2)
    assert rules.reference_date(date(2026, 1, 1), None) == date(2026, 1, 1)


def test_current_documents_latest_wins():
    old = Doc(K.vet_passport, T0, date(2026, 1, 1))
    new = Doc(K.vet_passport, T0 + timedelta(days=1), date(2027, 1, 1))
    cur = rules.current_documents([new, old])
    assert cur[K.vet_passport] is new


def test_problems_all_missing():
    assert rules.document_problems({}, REF, "open") == [
        rules.VET_PASSPORT_MISSING, rules.PEDIGREE_MISSING,
    ]


def test_problems_expired_on_reference_date():
    cur = rules.current_documents([
        Doc(K.vet_passport, T0, REF - timedelta(days=1)),
        Doc(K.puppy_card, T0),
    ])
    assert rules.document_problems(cur, REF, "open") == [rules.RABIES_EXPIRED]


def test_problems_valid_until_equal_reference_ok_and_date_missing():
    ok = rules.current_documents([Doc(K.vet_passport, T0, REF), Doc(K.pedigree, T0)])
    assert rules.document_problems(ok, REF, "open") == []
    no_date = rules.current_documents([Doc(K.vet_passport, T0), Doc(K.pedigree, T0)])
    assert rules.document_problems(no_date, REF, "open") == [rules.RABIES_DATE_MISSING]


def test_working_class_requires_certificate():
    cur = rules.current_documents([Doc(K.vet_passport, T0, REF), Doc(K.pedigree, T0)])
    assert rules.document_problems(cur, REF, "working") == [rules.WORKING_CERTIFICATE_MISSING]
    cur[K.working_certificate] = Doc(K.working_certificate, T0)
    assert rules.document_problems(cur, REF, "working") == []


def test_rabies_valid_for():
    assert rules.rabies_valid_for({}, REF) is None
    cur = rules.current_documents([Doc(K.vet_passport, T0, REF)])
    assert rules.rabies_valid_for(cur, REF) is True
    assert rules.rabies_valid_for(cur, REF + timedelta(days=1)) is False


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ({}, A.registered, A.registered),
        ({}, A.absent, A.absent),
        ({C.arrival: R.passed}, A.registered, A.arrived),
        ({C.arrival: R.passed}, A.absent, A.arrived),
        ({C.arrival: R.passed, C.vet: R.passed}, A.registered, A.arrived),
        ({C.arrival: R.passed, C.vet: R.passed, C.docs_onsite: R.passed}, A.arrived, A.admitted),
        ({C.arrival: R.passed, C.vet: R.failed}, A.arrived, A.rejected),
        ({C.arrival: R.passed, C.vet: R.passed, C.docs_onsite: R.failed}, A.admitted, A.rejected),
        ({C.vet: R.failed}, A.registered, A.rejected),
        ({C.arrival: R.failed}, A.arrived, A.registered),
        ({C.docs_precheck: R.passed}, A.registered, A.registered),
    ],
)
def test_compute_attendance_status(latest, current, expected):
    assert rules.compute_attendance_status(latest, current) == expected


def test_allowed_statuses_for():
    from app.models.show import ShowStatus as S
    assert rules.allowed_statuses_for(C.docs_precheck) == {S.registration_open, S.registration_closed}
    assert rules.allowed_statuses_for(C.vet) == {S.registration_closed, S.in_progress}
