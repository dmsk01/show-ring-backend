"""
Удаление аккаунта (право на уничтожение ПДн: ст. 14, 21 152-ФЗ).

Строка users не удаляется физически — на неё ссылаются выставки, записи
и рекламные кампании (FK RESTRICT, историчность мероприятий). Вместо
этого аккаунт обезличивается: из строки и связанных таблиц уходят все
персональные данные, вход невозможен.
"""

from __future__ import annotations

import random
import re
import uuid
from datetime import date, timedelta

import pytest_asyncio
from sqlalchemy import select

from app.main import app
from app.models.classified import Classified, ClassifiedImage
from app.models.consent import UserConsent
from app.models.dog import Dog, DogDocument, DogDocumentKind, SexEnum
from app.models.file import UploadedFile
from app.models.kennel import Kennel
from app.models.outbox import OutboxEvent
from app.models.show import ShowStatus
from app.models.user import RefreshToken, User, UserProfile
from app.services.sms import SMSProvider, get_sms_provider
from tests.integration.checkin_helpers import (
    PASSWORD,
    auth,
    make_api_user,
    make_references,
    make_world,
)

CONSENTS = {"accept_terms": True, "personal_data_consent": True}


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
async def sms():
    provider = _CaptureSMS()
    app.dependency_overrides[get_sms_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_sms_provider, None)


async def _phone_user(client, sms) -> tuple[str, str]:
    phone = _phone()
    await client.post("/auth/send-code", json={"phone": phone})
    r = await client.post(
        "/auth/verify-code",
        json={"phone": phone, "code": sms.last_code(), **CONSENTS},
        headers={"X-Token-Delivery": "body"},
    )
    assert r.status_code == 200, r.text
    return phone, r.json()["access_token"]


async def _reauth_code(client, sms, token: str) -> str:
    r = await client.post("/users/me/reauth/send-code", headers=auth(token))
    assert r.status_code == 200, r.text
    return sms.last_code()


async def test_phone_user_deletes_account(client, sms, db_session, test_redis):
    phone, token = await _phone_user(client, sms)
    me = (await client.get("/users/me", headers=auth(token))).json()
    uid = uuid.UUID(me["id"])

    # Наполняем аккаунт персональными данными.
    await client.patch(
        "/users/me/profile",
        json={"last_name": "Иванов", "first_name": "Иван"},
        headers=auth(token),
    )
    r = await client.post(
        "/kennels",
        json={
            "name": f"K {uuid.uuid4().hex[:6]}",
            "contact_phone": phone,
            "contact_email": "k@example.com",
            "website": "https://example.com",
            "contacts_public": True,
        },
        headers=auth(token),
    )
    kennel_id = uuid.UUID(r.json()["id"])
    r = await client.post(
        "/classifieds",
        json={
            "category": "other",
            "title": "Груминг",
            "description": "Стрижка собак любых пород",
            "price_kind": "negotiable",
        },
        headers=auth(token),
    )
    classified_id = uuid.UUID(r.json()["id"])
    # BE-11: фото объявления (часто — дом продавца) и письмо в outbox с
    # адресом пользователя тоже должны исчезнуть вместе с аккаунтом.
    photo = UploadedFile(
        uploaded_by=uid, s3_key=f"classifieds/{uuid.uuid4()}.jpg",
        original_filename="home.jpg", content_type="image/jpeg", size_bytes=10,
    )
    db_session.add(photo)
    await db_session.flush()
    db_session.add(ClassifiedImage(classified_id=classified_id, file_id=photo.id))
    me_email = f"del_{uuid.uuid4().hex[:6]}@example.com"
    user_row = await db_session.get(User, uid)
    user_row.email = me_email
    mail = OutboxEvent(
        routing_key="email_tasks",
        payload={"to_email": me_email, "html_body": "token=abc"},
    )
    db_session.add(mail)
    await db_session.commit()
    photo_id, mail_id = photo.id, mail.id
    breed, _, _ = await make_references(db_session)
    dog = Dog(
        breed_id=breed.id, name="Рекс", sex=SexEnum.male, owner_id=uid,
        date_of_birth=date.today() - timedelta(days=500),
    )
    scan = UploadedFile(
        uploaded_by=uid, s3_key=f"test/{uuid.uuid4()}.pdf",
        original_filename="vet.pdf", content_type="application/pdf",
        size_bytes=10, is_public=False,
    )
    db_session.add_all([dog, scan])
    await db_session.flush()
    doc = DogDocument(
        dog_id=dog.id, file_id=scan.id, kind=DogDocumentKind.vet_passport,
        uploaded_by=uid,
    )
    db_session.add(doc)
    await db_session.commit()
    doc_id, scan_id, dog_id = doc.id, scan.id, dog.id

    code = await _reauth_code(client, sms, token)
    r = await client.post(
        "/users/me/delete", json={"code": code}, headers=auth(token)
    )
    assert r.status_code == 200, r.text

    # Сессия больше не работает.
    assert (await client.get("/users/me", headers=auth(token))).status_code == 401

    db_session.expire_all()
    user = await db_session.get(User, uid)
    assert user is not None
    assert user.phone is None
    assert user.is_active is False
    assert user.deleted_at is not None
    assert user.hashed_password is None
    assert user.email is not None and user.email.endswith(".invalid")
    assert await db_session.get(UserProfile, uid) is None
    assert await db_session.get(Classified, classified_id) is None

    kennel = await db_session.get(Kennel, kennel_id)
    assert kennel.contact_phone is None
    assert kennel.contact_email is None
    assert kennel.website is None
    assert kennel.contacts_public is False

    assert await db_session.get(UploadedFile, photo_id) is None
    assert await db_session.get(OutboxEvent, mail_id) is None
    assert await db_session.get(DogDocument, doc_id) is None
    assert await db_session.get(UploadedFile, scan_id) is None
    # Сама собака — историческая запись (родословные, результаты), но
    # уже без привязки к человеку.
    dog_row = await db_session.get(Dog, dog_id)
    assert dog_row is not None and dog_row.owner_id is None

    for model in (UserConsent, RefreshToken):
        rows = await db_session.execute(select(model).where(model.user_id == uid))
        assert rows.scalars().first() is None

    # Номер освобождён: повторный вход создаёт НОВЫЙ аккаунт.
    await test_redis.delete(f"otp:login:cooldown:{phone}")
    await client.post("/auth/send-code", json={"phone": phone})
    r = await client.post(
        "/auth/verify-code",
        json={"phone": phone, "code": sms.last_code(), **CONSENTS},
        headers={"X-Token-Delivery": "body"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_new_user"] is True


async def test_wrong_code_keeps_account(client, sms):
    _, token = await _phone_user(client, sms)
    await _reauth_code(client, sms, token)

    r = await client.post(
        "/users/me/delete", json={"code": "000000"}, headers=auth(token)
    )
    assert r.status_code == 400
    assert (await client.get("/users/me", headers=auth(token))).status_code == 200


async def test_email_user_deletes_with_password(client):
    _, token = await make_api_user(client)

    r = await client.post(
        "/users/me/delete", json={"password": "wrong-pass"}, headers=auth(token)
    )
    assert r.status_code == 403

    r = await client.post(
        "/users/me/delete", json={"password": PASSWORD}, headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert (await client.get("/users/me", headers=auth(token))).status_code == 401


async def test_organizer_with_unfinished_show_cannot_delete(client, db_session):
    uid, token = await make_api_user(client)
    organizer = await db_session.get(User, uid)
    await make_world(
        db_session,
        organizer=organizer,
        status=ShowStatus.registration_open,
        date_start=date.today() + timedelta(days=10),
    )

    r = await client.post(
        "/users/me/delete", json={"password": PASSWORD}, headers=auth(token)
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "active_shows"
