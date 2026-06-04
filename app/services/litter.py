"""
Сервис помётов (этап 5).

Бизнес-правила:
- Помёт создаёт только владелец питомника (или admin).
- Родители (если указаны) — реальные собаки. father=male, mother=female.
- Породы родителей и помёта должны совпадать (обычно): валидация мягкая —
  предупреждение через ValueError, чтобы не блокировать редкие
  межпородные случаи. В этап 5 строгая валидация: совпадают или ошибка.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import SexEnum
from app.models.litter import Litter
from app.repositories import dog as dog_repo
from app.repositories import kennel as kennel_repo
from app.repositories import litter as repo
from app.schemas.notification import EventMessage
from app.services import notification as notif_svc


async def _check_kennel_owner(
    db: AsyncSession,
    kennel_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    kennel = await kennel_repo.get_kennel(db, kennel_id)
    if kennel is None:
        raise ValueError("kennel_not_found")
    if kennel.owner_id != requester_id and not is_admin:
        raise ValueError("forbidden")


async def _validate_parents(
    db: AsyncSession,
    breed_id: uuid.UUID,
    father_id: uuid.UUID | None,
    mother_id: uuid.UUID | None,
) -> None:
    if father_id is not None:
        father = await dog_repo.get_dog(db, father_id)
        if father is None:
            raise ValueError("father_not_found")
        if father.sex != SexEnum.male:
            raise ValueError("father_must_be_male")
        if father.breed_id != breed_id:
            # Жёсткая проверка: помёт зарегистрирован под одной породой,
            # родители должны быть той же. В реальном РКФ-учёте
            # межпородные помёты — отдельная процедура и обычно не
            # попадают в публичную доску.
            raise ValueError("father_breed_mismatch")
    if mother_id is not None:
        mother = await dog_repo.get_dog(db, mother_id)
        if mother is None:
            raise ValueError("mother_not_found")
        if mother.sex != SexEnum.female:
            raise ValueError("mother_must_be_female")
        if mother.breed_id != breed_id:
            raise ValueError("mother_breed_mismatch")


async def create_litter(
    db: AsyncSession,
    requester_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> Litter:
    await _check_kennel_owner(
        db, fields["kennel_id"], requester_id, is_admin
    )
    await _validate_parents(
        db,
        breed_id=fields["breed_id"],
        father_id=fields.get("father_id"),
        mother_id=fields.get("mother_id"),
    )
    obj = await repo.create_litter(db, **fields)

    # Подгружаем питомник и породу для красивого письма подписчикам.
    # Делаем это до commit'а — outbox enqueue должен быть в той же
    # транзакции, что и INSERT помёта.
    kennel = await kennel_repo.get_kennel(db, obj.kennel_id)
    from app.models.reference import Breed

    breed = await db.get(Breed, obj.breed_id)

    # routing_key с включением breed_id даёт подписчикам возможность
    # биндиться на конкретную породу через pattern:
    # "litter.announced.breed.<breed_id>". Сейчас воркер слушает "#",
    # но routing_key уже под расширение.
    #
    # publish_event с db=db → запись в outbox_events. Транзакционная
    # гарантия: либо помёт + событие, либо ничего. Без db (старый
    # вариант) при сбое Rabbit событие терялось бы.
    await notif_svc.publish_event(
        EventMessage(
            event_type="litter.announced",
            routing_key=f"litter.announced.breed.{obj.breed_id}",
            actor_id=requester_id,
            payload={
                "litter_id": str(obj.id),
                "kennel_name": kennel.name if kennel else "",
                "breed_id": str(obj.breed_id),
                "breed_name": breed.name if breed else "",
                "puppies_count": obj.puppies_count,
                "price_from": (
                    str(obj.price_from) if obj.price_from is not None else None
                ),
                "price_to": (
                    str(obj.price_to) if obj.price_to is not None else None
                ),
            },
        ),
        db=db,
    )

    await db.commit()
    await db.refresh(obj)

    return obj


async def update_litter(
    db: AsyncSession,
    litter_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> Litter:
    obj = await repo.get_litter(db, litter_id)
    if obj is None:
        raise ValueError("not_found")

    # Право: только владелец питомника (или admin).
    await _check_kennel_owner(db, obj.kennel_id, requester_id, is_admin)

    # Если меняются родители — проверяем заново на текущей породе помёта.
    if "father_id" in fields or "mother_id" in fields:
        await _validate_parents(
            db,
            breed_id=obj.breed_id,
            father_id=fields.get("father_id", obj.father_id),
            mother_id=fields.get("mother_id", obj.mother_id),
        )

    for k, v in fields.items():
        setattr(obj, k, v)

    await db.commit()
    await db.refresh(obj)
    return obj


async def delete_litter(
    db: AsyncSession,
    litter_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    obj = await repo.get_litter(db, litter_id)
    if obj is None:
        raise ValueError("not_found")
    # Право (как в update_litter): владелец питомника помёта или admin.
    await _check_kennel_owner(db, obj.kennel_id, requester_id, is_admin)
    # dogs.litter_id и classifieds.litter_id → SET NULL: щенки и
    # объявления переживают удаление помёта, теряя лишь привязку к нему.
    await db.delete(obj)
    await db.commit()
