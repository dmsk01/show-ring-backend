"""
Интеграция: правки по review 2026-06-10 — минорные по выставкам.

- publish_results обязан публиковать show.results_published (раньше
  событие слал только PUT /status, а /publish — нет: подписчики
  узнавали о результатах в зависимости от того, какой эндпоинт выбрал
  организатор).
- handler_id при записи/правке записи валидируется (раньше несуществующий
  UUID хендлера ронял 500 через IntegrityError на FK).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.outbox import OutboxEvent
from app.models.reference import Breed, ShowClass, ShowRank
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User
from app.services import result as result_svc
from app.services import show as show_svc


async def _breed(db_session) -> Breed:
    b = (await db_session.execute(select(Breed).limit(1))).scalars().first()
    if b is None:
        pytest.skip("нет пород (сиды) — пропускаем")
    return b


async def _rank(db_session) -> ShowRank:
    r = (await db_session.execute(select(ShowRank).limit(1))).scalars().first()
    if r is None:
        pytest.skip("нет рангов (сиды) — пропускаем")
    return r


async def _user(db_session) -> User:
    u = User(email=f"smf_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db_session.add(u)
    await db_session.commit()
    return u


async def _show_class(db_session, animal_type_id) -> ShowClass:
    c = ShowClass(
        animal_type_id=animal_type_id, code=f"OPEN{uuid.uuid4().hex[:4]}",
        name="Открытый", age_from_months=15, age_to_months=None,
    )
    db_session.add(c)
    await db_session.commit()
    return c


async def test_publish_results_emits_outbox_event(db_session):
    rank = await _rank(db_session)
    organizer = await _user(db_session)
    show = Show(
        organizer_id=organizer.id, name="Финальная", rank_id=rank.id,
        date_start=date.today(), status=ShowStatus.in_progress,
    )
    db_session.add(show)
    await db_session.commit()

    await result_svc.publish_results(
        db_session, show_id=show.id, user_id=organizer.id, is_admin=False
    )

    rows = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.routing_key == "show.results_published"
            )
        )
    ).scalars().all()
    ours = [r for r in rows if r.payload.get("payload", {}).get("show_id") == str(show.id)]
    assert ours, "publish_results не записал show.results_published в outbox"


async def test_register_entry_unknown_handler_rejected(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    owner = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(
        breed_id=breed.id, name="С хендлером", sex=SexEnum.male,
        date_of_birth=date.today() - timedelta(days=600),
        owner_id=owner.id,
    )
    db_session.add(dog)
    show = Show(
        organizer_id=owner.id, name="Запись", rank_id=rank.id,
        date_start=date.today() + timedelta(days=5),
        status=ShowStatus.registration_open,
    )
    db_session.add(show)
    await db_session.commit()

    with pytest.raises(ValueError, match="handler_not_found"):
        await show_svc.register_entry(
            db_session,
            show_id=show.id,
            requester_id=owner.id,
            is_admin=False,
            dog_id=dog.id,
            show_class_id=cls.id,
            handler_id=uuid.uuid4(),  # несуществующий пользователь
            notes=None,
            today=date.today(),
        )


async def test_update_entry_unknown_handler_rejected(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    owner = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(
        breed_id=breed.id, name="Правка хендлера", sex=SexEnum.male,
        date_of_birth=date.today() - timedelta(days=600),
        owner_id=owner.id,
    )
    db_session.add(dog)
    show = Show(
        organizer_id=owner.id, name="Правка записи", rank_id=rank.id,
        date_start=date.today() + timedelta(days=5),
        status=ShowStatus.registration_open,
    )
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(
        show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
        registered_by=owner.id,
    )
    db_session.add(entry)
    await db_session.commit()

    with pytest.raises(ValueError, match="handler_not_found"):
        await show_svc.update_entry(
            db_session,
            show_id=show.id,
            entry_id=entry.id,
            requester_id=owner.id,
            is_admin=False,
            show_class_id=None,
            handler_id=uuid.uuid4(),
            notes=None,
            today=date.today(),
        )
