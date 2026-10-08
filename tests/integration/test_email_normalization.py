"""
Интеграция: нормализация email и согласия при регистрации
(ревью 2026-10-06, BE-16 и BE-17).

BE-16: "User@Mail.ru" и "user@mail.ru" — один аккаунт; вход не зависит от
регистра; БД не допускает двух адресов, отличающихся регистром.
BE-17: регистрация по email без обязательных согласий отклоняется
(как и вход по телефону) — иначе аккаунт создаётся в обход 152-ФЗ.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.consent import UserConsent
from app.models.user import User

PASSWORD = "secret123"
CONSENTS = {"accept_terms": True, "personal_data_consent": True}


def _mixed_email() -> str:
    return f"Mixed.Case_{uuid.uuid4().hex[:8]}@Example.COM"


async def test_register_stores_lowercase_and_login_is_case_insensitive(client, db_session):
    email = _mixed_email()
    r = await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD, **CONSENTS}
    )
    assert r.status_code == 200, r.text
    stored = (
        await db_session.execute(
            select(User.email).where(func.lower(User.email) == email.lower())
        )
    ).scalar_one()
    assert stored == email.lower()

    for variant in (email, email.lower(), email.upper()):
        r = await client.post(
            "/auth/login",
            json={"email": variant, "password": PASSWORD},
            headers={"X-Token-Delivery": "body"},
        )
        assert r.status_code == 200, (variant, r.text)


async def test_case_variant_does_not_create_second_account(client, db_session):
    email = _mixed_email()
    for variant in (email, email.lower()):
        r = await client.post(
            "/auth/register",
            json={"email": variant, "password": PASSWORD, **CONSENTS},
        )
        assert r.status_code == 200, r.text
    count = (
        await db_session.execute(
            select(func.count()).select_from(User).where(
                func.lower(User.email) == email.lower()
            )
        )
    ).scalar_one()
    assert count == 1


async def test_db_rejects_case_duplicate_emails(db_session):
    email = _mixed_email()
    db_session.add(User(email=email, hashed_password="x"))
    await db_session.commit()
    db_session.add(User(email=email.lower(), hashed_password="x"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_register_without_consents_rejected(client, db_session):
    email = _mixed_email()
    r = await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "consent_required"
    exists = (
        await db_session.execute(
            select(func.count()).select_from(User).where(
                func.lower(User.email) == email.lower()
            )
        )
    ).scalar_one()
    assert exists == 0


async def test_register_writes_consents_with_user(client, db_session):
    email = _mixed_email()
    r = await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD, **CONSENTS}
    )
    assert r.status_code == 200, r.text
    user_id = (
        await db_session.execute(
            select(User.id).where(func.lower(User.email) == email.lower())
        )
    ).scalar_one()
    kinds = set(
        (
            await db_session.execute(
                select(UserConsent.kind).where(UserConsent.user_id == user_id)
            )
        ).scalars()
    )
    assert {"terms", "personal_data"} <= kinds
