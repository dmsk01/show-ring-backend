"""
Сервис выставок (этап 6).

Бизнес-правила:
- Создавать выставку может только organizer или admin.
- Редактировать/удалять — только организатор-владелец (или admin).
- Переходы статусов через ALLOWED_TRANSITIONS (см. show_rules.py).
- Запись на выставку:
  * status выставки == registration_open
  * сегодня <= registration_deadline (если задан)
  * порода собаки допущена (allow-list или всепородная)
  * выбранный класс соответствует возрасту собаки
  * собака не записана дважды (UNIQUE + явная проверка)
  * запись делает владелец питомника собаки (или admin)
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog
from app.models.reference import Breed, ShowClass
from app.models.show import (
    Show,
    ShowEntry,
    ShowJudge,
    ShowRing,
    ShowStatus,
)
from app.repositories import dog as dog_repo
from app.repositories import kennel as kennel_repo
from app.repositories import show as repo
from app.schemas.notification import EventMessage
from app.services import notification as notif_svc
from app.services import show_rules


# ---------------------------------------------------------------------
# Show (CRUD + status)
# ---------------------------------------------------------------------


async def _ensure_organizer_owner(
    show: Show, requester_id: uuid.UUID, is_admin: bool
) -> None:
    if show.organizer_id != requester_id and not is_admin:
        raise ValueError("forbidden")


async def create_show(
    db: AsyncSession,
    organizer_id: uuid.UUID,
    fields: dict,
) -> Show:
    breed_ids: list[uuid.UUID] = fields.pop("breed_ids", []) or []
    obj = await repo.create_show(db, organizer_id=organizer_id, **fields)
    for bid in breed_ids:
        await repo.add_show_breed(db, show_id=obj.id, breed_id=bid)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update_show(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> Show:
    obj = await repo.get_show(db, show_id)
    if obj is None:
        raise ValueError("not_found")
    await _ensure_organizer_owner(obj, requester_id, is_admin)
    # Менять название/город/даты можно только в draft и registration_open.
    # После закрытия регистрации это уже зафиксированные данные каталога.
    if obj.status not in (ShowStatus.draft, ShowStatus.registration_open):
        raise ValueError("show_locked")
    for k, v in fields.items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return obj


async def delete_show(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    obj = await repo.get_show(db, show_id)
    if obj is None:
        raise ValueError("not_found")
    await _ensure_organizer_owner(obj, requester_id, is_admin)
    # Жёсткое удаление разрешено только для draft и cancelled. Активные и
    # завершённые выставки несут записи/результаты/титулы — это история,
    # которую нельзя терять. Для боевых выставок есть статус cancelled
    # (PUT /shows/{id}/status). На completed БД и так заблокировала бы
    # удаление: dog_titles.show_id = ON DELETE RESTRICT.
    if obj.status not in (ShowStatus.draft, ShowStatus.cancelled):
        raise ValueError("show_locked")
    # Каскад (breeds/judges/rings/entries) отрабатывает на уровне ORM
    # (delete-orphan) и БД (ON DELETE CASCADE) — связанные строки уйдут.
    await db.delete(obj)
    await db.commit()


async def change_status(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    target: ShowStatus,
) -> Show:
    obj = await repo.get_show(db, show_id)
    if obj is None:
        raise ValueError("not_found")
    await _ensure_organizer_owner(obj, requester_id, is_admin)

    if not show_rules.is_transition_allowed(obj.status, target):
        raise ValueError("invalid_status_transition")

    # При закрытии регистрации — присваиваем номера каталога тем
    # записям, у которых их ещё нет (порядок по created_at).
    if target == ShowStatus.registration_closed:
        await _assign_catalog_numbers(db, show_id)

    obj.status = target

    # Публикуем событие ДО commit'а, через transactional outbox.
    # INSERT в outbox_events идёт в той же транзакции, что и UPDATE
    # status — гарантия "событие появилось ⇔ статус действительно
    # изменился". Раньше publish был после commit (fire-and-forget),
    # что могло терять события при падении Rabbit.
    if target == ShowStatus.registration_open:
        await notif_svc.publish_event(
            EventMessage(
                event_type="show.registration_opened",
                routing_key="show.registration_opened",
                actor_id=requester_id,
                payload={
                    "show_id": str(obj.id),
                    "show_name": obj.name,
                    "show_rank": "",  # rank.name догрузит хендлер событий, если надо
                    "date_start": obj.date_start.isoformat(),
                    "city": obj.city,
                    "registration_deadline": (
                        obj.registration_deadline.isoformat()
                        if obj.registration_deadline
                        else None
                    ),
                },
            ),
            db=db,
        )
    elif target == ShowStatus.completed:
        await notif_svc.publish_event(
            EventMessage(
                event_type="show.results_published",
                routing_key="show.results_published",
                actor_id=requester_id,
                payload={
                    "show_id": str(obj.id),
                    "show_name": obj.name,
                    "date_start": obj.date_start.isoformat(),
                    "show_rank": "",
                },
            ),
            db=db,
        )

    await db.commit()
    await db.refresh(obj)
    return obj


async def _assign_catalog_numbers(
    db: AsyncSession, show_id: uuid.UUID
) -> None:
    """
    Прогоняет все ShowEntry выставки и присваивает catalog_number тем,
    у кого его ещё нет. Порядок — по дате создания записи (FIFO).
    """
    entries = await repo.list_show_entries(db, show_id, page=1, per_page=10_000)
    next_num = await repo.next_catalog_number(db, show_id)
    for entry in entries:
        if entry.catalog_number is None:
            entry.catalog_number = next_num
            next_num += 1


# ---------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------


async def add_judge(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    *,
    judge_id: uuid.UUID,
    breed_id: uuid.UUID | None,
    breed_group_id: uuid.UUID | None,
) -> ShowJudge:
    show = await repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    await _ensure_organizer_owner(show, requester_id, is_admin)
    if show.status not in (ShowStatus.draft, ShowStatus.registration_open):
        raise ValueError("show_locked")
    obj = await repo.add_show_judge(
        db,
        show_id=show_id,
        judge_id=judge_id,
        breed_id=breed_id,
        breed_group_id=breed_group_id,
    )
    await db.commit()
    return obj


async def remove_judge(
    db: AsyncSession,
    show_id: uuid.UUID,
    judge_record_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    show = await repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    await _ensure_organizer_owner(show, requester_id, is_admin)
    judge = await repo.get_show_judge(db, judge_record_id)
    if judge is None or judge.show_id != show_id:
        raise ValueError("judge_assignment_not_found")
    await db.delete(judge)
    await db.commit()


# ---------------------------------------------------------------------
# Rings
# ---------------------------------------------------------------------


async def add_ring(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> ShowRing:
    show = await repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    await _ensure_organizer_owner(show, requester_id, is_admin)
    obj = await repo.create_show_ring(db, show_id=show_id, **fields)
    await db.commit()
    return obj


async def update_ring(
    db: AsyncSession,
    show_id: uuid.UUID,
    ring_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> ShowRing:
    show = await repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    await _ensure_organizer_owner(show, requester_id, is_admin)
    ring = await repo.get_show_ring(db, ring_id)
    if ring is None or ring.show_id != show_id:
        raise ValueError("ring_not_found")
    for k, v in fields.items():
        setattr(ring, k, v)
    await db.commit()
    return ring


async def delete_ring(
    db: AsyncSession,
    show_id: uuid.UUID,
    ring_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    show = await repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    await _ensure_organizer_owner(show, requester_id, is_admin)
    ring = await repo.get_show_ring(db, ring_id)
    if ring is None or ring.show_id != show_id:
        raise ValueError("ring_not_found")
    # Ринг — чистый элемент расписания: на show_rings никто не ссылается
    # (результаты привязаны к ShowEntry, не к рингу), поэтому удаление
    # безопасно в любом статусе. Симметрично add_ring/update_ring.
    await db.delete(ring)
    await db.commit()


# ---------------------------------------------------------------------
# Available classes
# ---------------------------------------------------------------------


async def get_available_classes_for_dog(
    db: AsyncSession,
    show_id: uuid.UUID,
    dog_id: uuid.UUID,
) -> tuple[Dog, list[show_rules.AvailableClassInfo], int]:
    """
    Возвращает (собака, список доступных классов, возраст в месяцах).
    Используется в GET /shows/{id}/available-classes/{dog_id}.
    """
    show = await repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    dog = await dog_repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("dog_not_found")
    if dog.date_of_birth is None:
        raise ValueError("dog_birth_date_missing")

    # animal_type определяем через породу. Загружаем breed чтобы знать,
    # к какому виду относится собака.
    breed = await db.get(Breed, dog.breed_id)
    if breed is None:
        raise ValueError("breed_not_found")

    age_months = show_rules.age_in_months_on(
        dog.date_of_birth, show.date_start
    )
    classes = await show_rules.list_available_classes_for_age(
        db, breed.animal_type_id, age_months
    )
    return dog, classes, age_months


# ---------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------


async def _check_can_register_dog(
    db: AsyncSession,
    show: Show,
    dog: Dog,
    requester_id: uuid.UUID,
    is_admin: bool,
    today: date,
) -> None:
    """Проверки правил записи. Бросает ValueError при провале."""
    if show.status != ShowStatus.registration_open:
        raise ValueError("registration_not_open")

    if (
        show.registration_deadline is not None
        and today > show.registration_deadline
    ):
        raise ValueError("registration_deadline_passed")

    # Записать собаку может её владелец (dog.owner_id) или admin. Для
    # легаси-собак без owner_id (бэкафилл не сопоставил) — фолбэк на
    # владельца питомника, как раньше. Это закрывает запись чужой собаки:
    # без права на собаку — forbidden.
    if not is_admin:
        is_owner = dog.owner_id is not None and dog.owner_id == requester_id
        if not is_owner:
            if dog.kennel_id is None:
                raise ValueError("forbidden")
            kennel = await kennel_repo.get_kennel(db, dog.kennel_id)
            if kennel is None or kennel.owner_id != requester_id:
                raise ValueError("forbidden")

    allowed = await repo.is_breed_allowed(db, show.id, dog.breed_id)
    if not allowed:
        raise ValueError("breed_not_allowed")


async def register_entry(
    db: AsyncSession,
    show_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    *,
    dog_id: uuid.UUID,
    show_class_id: uuid.UUID,
    handler_id: uuid.UUID | None,
    notes: str | None,
    today: date,
) -> ShowEntry:
    # Берём SELECT FOR UPDATE — блокировка строки выставки до конца
    # транзакции. Это предотвращает race: два запроса записи одной собаки
    # одновременно проходят проверку "не записана" и оба INSERT-ят.
    # Дополнительно UNIQUE(show_id, dog_id) ловит дубликат на уровне БД.
    show = await repo.get_show_for_update(db, show_id)
    if show is None:
        raise ValueError("not_found")

    dog = await dog_repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("dog_not_found")
    if dog.date_of_birth is None:
        raise ValueError("dog_birth_date_missing")

    await _check_can_register_dog(
        db, show, dog, requester_id, is_admin, today
    )

    if await repo.is_dog_registered(db, show_id, dog_id):
        raise ValueError("dog_already_registered")

    # Проверяем выбранный класс: существует, относится к нужному
    # animal_type, проходит по возрасту.
    cls = await db.get(ShowClass, show_class_id)
    if cls is None:
        raise ValueError("show_class_not_found")

    breed = await db.get(Breed, dog.breed_id)
    if breed is None:
        raise ValueError("breed_not_found")
    if cls.animal_type_id != breed.animal_type_id:
        raise ValueError("class_animal_type_mismatch")

    age_months = show_rules.age_in_months_on(
        dog.date_of_birth, show.date_start
    )
    available = await show_rules.list_available_classes_for_age(
        db, breed.animal_type_id, age_months
    )
    if not any(c.id == show_class_id for c in available):
        raise ValueError("class_not_available_for_age")

    obj = await repo.create_show_entry(
        db,
        show_id=show_id,
        dog_id=dog_id,
        show_class_id=show_class_id,
        handler_id=handler_id,
        registered_by=requester_id,
        notes=notes,
    )
    await db.commit()
    return obj


async def cancel_entry(
    db: AsyncSession,
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    entry = await repo.get_show_entry(db, entry_id)
    if entry is None or entry.show_id != show_id:
        raise ValueError("entry_not_found")
    # Отменить запись может только тот, кто её сделал (или admin).
    if entry.registered_by != requester_id and not is_admin:
        raise ValueError("forbidden")
    # После закрытия регистрации отмену делает только организатор/admin.
    show = await repo.get_show(db, show_id)
    if show is not None and show.status not in (
        ShowStatus.draft,
        ShowStatus.registration_open,
    ):
        if not (is_admin or show.organizer_id == requester_id):
            raise ValueError("registration_locked")
    await db.delete(entry)
    await db.commit()


async def update_entry(
    db: AsyncSession,
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    *,
    show_class_id: uuid.UUID | None,
    handler_id: uuid.UUID | None,
    notes: str | None,
    today: date,
) -> ShowEntry:
    """Редактирование своей записи: класс/хендлер/заметки.

    catalog_number не трогаем. Менять собаку нельзя (это другая запись).
    Разрешено только пока регистрация открыта (иначе registration_locked).
    """
    entry = await repo.get_show_entry(db, entry_id)
    if entry is None or entry.show_id != show_id:
        raise ValueError("entry_not_found")
    if entry.registered_by != requester_id and not is_admin:
        raise ValueError("forbidden")

    show = await repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    if show.status != ShowStatus.registration_open and not is_admin:
        raise ValueError("registration_locked")

    # Смена класса — валидируем по возрасту собаки (как в register_entry).
    if show_class_id is not None and show_class_id != entry.show_class_id:
        cls = await db.get(ShowClass, show_class_id)
        if cls is None:
            raise ValueError("show_class_not_found")
        dog = await dog_repo.get_dog(db, entry.dog_id)
        if dog is None or dog.date_of_birth is None:
            raise ValueError("dog_birth_date_missing")
        breed = await db.get(Breed, dog.breed_id)
        if breed is None:
            raise ValueError("breed_not_found")
        if cls.animal_type_id != breed.animal_type_id:
            raise ValueError("class_animal_type_mismatch")
        age_months = show_rules.age_in_months_on(dog.date_of_birth, show.date_start)
        available = await show_rules.list_available_classes_for_age(
            db, breed.animal_type_id, age_months
        )
        if not any(c.id == show_class_id for c in available):
            raise ValueError("class_not_available_for_age")
        entry.show_class_id = show_class_id

    if handler_id is not None:
        entry.handler_id = handler_id
    if notes is not None:
        entry.notes = notes

    await db.commit()
    await db.refresh(entry)
    return entry
