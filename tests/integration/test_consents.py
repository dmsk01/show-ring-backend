"""
Журнал согласий (152-ФЗ): фиксация при входе по телефону, просмотр,
выдача и отзыв из личного кабинета.

Обязанность доказать получение согласия — на операторе (ч. 3 ст. 9
152-ФЗ), поэтому проверяем не только коды ответов, но и что строки
журнала реально записаны с редакцией документа и IP.
"""

from __future__ import annotations

import random
import re
import uuid

import pytest_asyncio
from sqlalchemy import select

from app.main import app
from app.models.consent import UserConsent
from app.models.user import User
from app.services.consent import CURRENT_REVISIONS, ConsentKind
from app.services.sms import SMSProvider, get_sms_provider
from tests.integration.checkin_helpers import auth, make_api_user

ALL_ACCOUNT_CONSENTS = {"accept_terms": True, "personal_data_consent": True}


def _phone() -> str:
    return f"+7999{random.randint(1000000, 9999999)}"


class _CaptureSMS(SMSProvider):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, phone: str, message: str) -> None:
        self.sent.append((phone, message))

    def last_code(self) -> str:
        match = re.search(r"\d{4,8}", self.sent[-1][1])
        assert match
        return match.group()


@pytest_asyncio.fixture
async def sms_capture():
    provider = _CaptureSMS()
    app.dependency_overrides[get_sms_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_sms_provider, None)


async def _code(client, sms, phone: str) -> str:
    r = await client.post("/auth/send-code", json={"phone": phone})
    assert r.status_code == 200, r.text
    return sms.last_code()


async def test_new_phone_user_without_consents_is_rejected(
    client, sms_capture, db_session
):
    phone = _phone()
    code = await _code(client, sms_capture, phone)

    r = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )

    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "consent_required"
    user = (
        await db_session.execute(select(User).where(User.phone == phone))
    ).scalar_one_or_none()
    assert user is None, "аккаунт без согласия создаваться не должен"


async def test_new_phone_user_with_consents_is_recorded(
    client, sms_capture, db_session
):
    phone = _phone()
    code = await _code(client, sms_capture, phone)

    r = await client.post(
        "/auth/verify-code",
        json={"phone": phone, "code": code, **ALL_ACCOUNT_CONSENTS},
        headers={"X-Token-Delivery": "body", "User-Agent": "pytest-ua"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    r = await client.get("/users/me/consents", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["missing"] == []
    kinds = {c["kind"]: c for c in body["active"]}
    assert set(kinds) == {"terms", "personal_data"}
    assert kinds["terms"]["revision"] == CURRENT_REVISIONS[ConsentKind.terms]

    rows = (
        await db_session.execute(
            select(UserConsent).where(
                UserConsent.kind == ConsentKind.personal_data.value
            ).order_by(UserConsent.granted_at.desc())
        )
    ).scalars().first()
    assert rows is not None
    assert rows.user_agent == "pytest-ua"


async def test_existing_phone_user_can_log_in_without_consents(
    client, sms_capture, test_redis
):
    # Повторный вход уже существующего пользователя не блокируем: недостающие
    # согласия он подтвердит в интерфейсе (GET /users/me/consents → missing).
    phone = _phone()
    code = await _code(client, sms_capture, phone)
    r = await client.post(
        "/auth/verify-code",
        json={"phone": phone, "code": code, **ALL_ACCOUNT_CONSENTS},
    )
    assert r.status_code == 200, r.text

    await test_redis.delete(f"otp:login:cooldown:{phone}")
    code = await _code(client, sms_capture, phone)
    r = await client.post(
        "/auth/verify-code", json={"phone": phone, "code": code}
    )
    assert r.status_code == 200, r.text


async def test_legacy_user_sees_missing_and_grants(client, db_session):
    # Пользователь, созданный без согласий (регистрация по email до правок).
    _, token = await make_api_user(client)

    r = await client.get("/users/me/consents", headers=auth(token))
    assert r.status_code == 200, r.text
    assert set(r.json()["missing"]) == {"terms", "personal_data"}

    r = await client.post(
        "/users/me/consents",
        json={"kinds": ["terms", "personal_data"]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["missing"] == []

    # Повторная выдача той же редакции не плодит строк (идемпотентно).
    r = await client.post(
        "/users/me/consents",
        json={"kinds": ["terms", "personal_data"]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    me = await client.get("/users/me", headers=auth(token))
    uid = uuid.UUID(me.json()["id"])
    count = len(
        (
            await db_session.execute(
                select(UserConsent).where(UserConsent.user_id == uid)
            )
        ).scalars().all()
    )
    assert count == 2


async def test_revoke_personal_data_consent(client):
    _, token = await make_api_user(client)
    await client.post(
        "/users/me/consents",
        json={"kinds": ["terms", "personal_data"]},
        headers=auth(token),
    )

    r = await client.delete(
        "/users/me/consents/personal_data", headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["missing"] == ["personal_data"]


async def test_terms_cannot_be_revoked_separately(client):
    # Отказ от Соглашения = удаление аккаунта (п. 16.2), отдельного
    # «отзыва» нет — иначе аккаунт жил бы без договора.
    _, token = await make_api_user(client)
    r = await client.delete("/users/me/consents/terms", headers=auth(token))
    assert r.status_code == 400
    assert r.json()["detail"] == "use_account_deletion"


async def test_public_kinds_not_grantable_via_account_endpoint(client):
    # Согласие на распространение даётся по конкретной публикации
    # (contacts_public у питомника/объявления), а не «вообще».
    _, token = await make_api_user(client)
    r = await client.post(
        "/users/me/consents",
        json={"kinds": ["public_kennel_contacts"]},
        headers=auth(token),
    )
    assert r.status_code == 422
