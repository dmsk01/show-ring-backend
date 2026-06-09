# "My Shows" Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the three backend API changes that power the frontend "Мои выставки" (My Shows) section: an aggregate list of shows the current user has entries in, enriched per-show "my entries", and editing an own entry.

**Architecture:** Standard layered FastAPI app (`app/models`, `app/repositories`, `app/schemas`, `app/services`, `app/routers`). New read query (aggregate shows-with-my-entry-count) and enrichment join (dog/class names) go in the repository; the PATCH validation logic goes in the service mirroring `register_entry`. Routers stay thin and translate `ValueError` codes to HTTP via the existing `_raise_for_error`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async), Pydantic v2, pytest + pytest-asyncio. Repo: `E:\Coding\python-animal-platform`.

**Conventions verified:**
- Entry endpoints live in `app/routers/shows.py` under the `# Entries` section. `/shows/{show_id}/entries/my` is registered BEFORE `/shows/{show_id}/entries/{entry_id}` so "my" isn't parsed as a UUID.
- `ShowEntry` (`app/models/show.py`) has only a `show` relationship — no `dog`/`show_class` relationships. Names require explicit joins to `Dog` (`app/models/dog.py`, field `name`) and `ShowClass` (`app/models/reference.py`, fields `code`, `name`).
- Service functions raise `ValueError("<code>")`; router catches and calls `_raise_for_error` (`app/routers/shows.py:50`). `registration_locked` → 422, `forbidden` → 403, `entry_not_found` → 404, `class_not_available_for_age`/`class_animal_type_mismatch` → 422.
- `ShowResponse` schema is in `app/schemas/show.py`; show status enum `ShowStatus` in `app/models/show.py`.
- Tests use `db_session` and `client` fixtures from `tests/integration/conftest.py`. Repository/service-level tests instantiate models directly via `db_session` (see `tests/integration/test_showcase.py` helpers `_owner`, `_breed_id`). Run tests: `pytest <path> -v`.

> **NOTE on status code for locked edit:** the spec mentioned 409, but the existing convention maps `registration_locked` → 422. This plan uses **422** to stay consistent. The frontend treats any 4xx as a toast error, so behavior is unchanged.

---

## File Structure

- `app/schemas/show.py` — add `MyShowEntryResponse`, `ShowEntryUpdate`, `MyShowItem`, `MyShowPage`.
- `app/repositories/show.py` — add `list_user_entries_for_show_enriched`, `get_entry_enriched`, `list_my_shows`, `count_my_shows`.
- `app/services/show.py` — add `update_entry`.
- `app/routers/shows.py` — add `GET /shows/entries/my`; change `list_my_entries` to return enriched; add `PATCH /shows/{show_id}/entries/{entry_id}`.
- `tests/integration/test_my_shows.py` — new test module (repo + service + router behaviors).

---

## Task 1: Schemas

**Files:**
- Modify: `app/schemas/show.py` (Entries section, after `ShowEntryPage` ~line 211; reuse existing `ShowResponse`)

- [ ] **Step 1: Add the new schemas**

Append after `ShowEntryPage` in `app/schemas/show.py`:

```python
class MyShowEntryResponse(ShowEntryResponse):
    """Запись + имена собаки и класса (для страницы «мои записи»)."""
    dog_name: str
    class_code: str
    class_name: str


class ShowEntryUpdate(BaseModel):
    show_class_id: uuid.UUID | None = None
    handler_id: uuid.UUID | None = None
    notes: str | None = None


class MyShowItem(ShowResponse):
    """Выставка, где у пользователя есть запись + счётчик его записей."""
    my_entries_count: int


class MyShowPage(BaseModel):
    items: list[MyShowItem]
    total: int
    page: int
    per_page: int
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "import app.schemas.show as s; print(s.MyShowItem, s.MyShowEntryResponse, s.ShowEntryUpdate, s.MyShowPage)"`
Expected: prints the four class objects, no ImportError. (If `ShowResponse` is named differently, grep `class Show.*Response` in `app/schemas/show.py` and use the canonical show output schema.)

- [ ] **Step 3: Commit**

```bash
git add app/schemas/show.py
git commit -m "feat(shows): schemas for my-shows aggregate + enriched entry + entry update"
```

---

## Task 2: Repository — enriched "my entries" for one show

**Files:**
- Modify: `app/repositories/show.py` (after `list_user_entries_for_show` ~line 294)
- Test: `tests/integration/test_my_shows.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_my_shows.py`:

```python
"""Интеграция: раздел «Мои выставки» (агрегат, обогащение, PATCH записи)."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.dog import Dog, SexEnum
from app.models.reference import Breed, ShowClass
from app.models.show import Show, ShowEntry, ShowStatus
from app.models.user import User
from app import repositories as _  # noqa: ensure package import side-effects
from app.repositories import show as repo


async def _breed(db_session):
    b = (await db_session.execute(select(Breed).limit(1))).scalars().first()
    if b is None:
        pytest.skip("нет пород (сиды) — пропускаем")
    return b


async def _user(db_session) -> User:
    u = User(email=f"my_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
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


async def _show(db_session, organizer_id, status=ShowStatus.registration_open) -> Show:
    rank_id = (await db_session.execute(
        select(Breed.id).limit(1))).scalar_one()  # placeholder; replace below
    raise NotImplementedError


async def test_list_user_entries_enriched_returns_names(db_session):
    breed = await _breed(db_session)
    user = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(breed_id=breed.id, name="Рекс Тест", sex=SexEnum.male)
    db_session.add(dog)
    await db_session.commit()

    # минимальная выставка
    show = Show(organizer_id=user.id, name="Выставка А",
                rank_id=_any_rank_id_sync, date_start=date.today())  # see note
    db_session.add(show)
    await db_session.commit()
    db_session.add(ShowEntry(show_id=show.id, dog_id=dog.id,
                             show_class_id=cls.id, registered_by=user.id))
    await db_session.commit()

    rows = await repo.list_user_entries_for_show_enriched(db_session, show.id, user.id)
    assert len(rows) == 1
    entry, dog_name, class_code, class_name = rows[0]
    assert dog_name == "Рекс Тест"
    assert class_name == "Открытый"
    assert class_code == cls.code
```

> **IMPLEMENTER NOTE (Show creation):** `Show` requires a valid `rank_id` (FK to `show_ranks`). Before writing assertions, add a helper `_rank(db_session)` that does `(await db_session.execute(select(ShowRank).limit(1))).scalars().first()` (import `ShowRank` from `app.models.reference`) and `pytest.skip` if absent, then build `Show(organizer_id=..., name=..., rank_id=rank.id, date_start=date.today(), status=...)`. Replace the `_show` placeholder and the `rank_id=_any_rank_id_sync` line accordingly. Confirm `Show` constructor required fields by reading `app/models/show.py` `class Show`. This note exists because seed data, not invented fixtures, must back the FK.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/integration/test_my_shows.py::test_list_user_entries_enriched_returns_names -v`
Expected: FAIL — `AttributeError: module 'app.repositories.show' has no attribute 'list_user_entries_for_show_enriched'`.

- [ ] **Step 3: Implement the repository function**

In `app/repositories/show.py`, add imports at top if missing (`from app.models.dog import Dog`, `from app.models.reference import ShowClass`) and append after `list_user_entries_for_show`:

```python
async def list_user_entries_for_show_enriched(
    db: AsyncSession, show_id: uuid.UUID, user_id: uuid.UUID
):
    """Записи пользователя на выставку с именами собаки и класса.

    Возвращает список кортежей (ShowEntry, dog_name, class_code, class_name).
    """
    stmt = (
        select(ShowEntry, Dog.name, ShowClass.code, ShowClass.name)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .join(ShowClass, ShowClass.id == ShowEntry.show_class_id)
        .where(
            ShowEntry.show_id == show_id,
            ShowEntry.registered_by == user_id,
        )
        .order_by(
            ShowEntry.catalog_number.asc().nullslast(),
            ShowEntry.created_at.asc(),
        )
    )
    return (await db.execute(stmt)).all()


async def get_entry_enriched(
    db: AsyncSession, entry_id: uuid.UUID
):
    """Одна запись + имена (для ответа PATCH). None, если не найдена."""
    stmt = (
        select(ShowEntry, Dog.name, ShowClass.code, ShowClass.name)
        .join(Dog, Dog.id == ShowEntry.dog_id)
        .join(ShowClass, ShowClass.id == ShowEntry.show_class_id)
        .where(ShowEntry.id == entry_id)
    )
    return (await db.execute(stmt)).first()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/integration/test_my_shows.py::test_list_user_entries_enriched_returns_names -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repositories/show.py tests/integration/test_my_shows.py
git commit -m "feat(shows): repo query for enriched user entries (dog/class names)"
```

---

## Task 3: Repository — aggregate "my shows"

**Files:**
- Modify: `app/repositories/show.py`
- Test: `tests/integration/test_my_shows.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_my_shows.py` (reuse helpers from Task 2):

```python
ACTIVE_STATUSES = (
    ShowStatus.registration_open,
    ShowStatus.registration_closed,
    ShowStatus.in_progress,
)
PAST_STATUSES = (ShowStatus.completed, ShowStatus.cancelled)


async def test_list_my_shows_groups_and_counts(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)            # helper added per Task 2 note
    user = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)

    def mk_dog(n):
        d = Dog(breed_id=breed.id, name=n, sex=SexEnum.male)
        db_session.add(d)
        return d

    d1, d2, d3 = mk_dog("D1"), mk_dog("D2"), mk_dog("D3")
    await db_session.commit()

    active = Show(organizer_id=user.id, name="Активная", rank_id=rank.id,
                  date_start=date.today(), status=ShowStatus.registration_open)
    past = Show(organizer_id=user.id, name="Прошедшая", rank_id=rank.id,
                date_start=date.today() - timedelta(days=30),
                status=ShowStatus.completed)
    db_session.add_all([active, past])
    await db_session.commit()

    # 2 записи на активную, 1 на прошедшую
    db_session.add_all([
        ShowEntry(show_id=active.id, dog_id=d1.id, show_class_id=cls.id, registered_by=user.id),
        ShowEntry(show_id=active.id, dog_id=d2.id, show_class_id=cls.id, registered_by=user.id),
        ShowEntry(show_id=past.id, dog_id=d3.id, show_class_id=cls.id, registered_by=user.id),
    ])
    await db_session.commit()

    rows = await repo.list_my_shows(db_session, user.id, "active", page=1, per_page=12)
    total = await repo.count_my_shows(db_session, user.id, "active")
    assert total == 1
    assert len(rows) == 1
    show_obj, count = rows[0]
    assert show_obj.id == active.id
    assert count == 2

    past_total = await repo.count_my_shows(db_session, user.id, "past")
    assert past_total == 1
    all_total = await repo.count_my_shows(db_session, user.id, "all")
    assert all_total == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/integration/test_my_shows.py::test_list_my_shows_groups_and_counts -v`
Expected: FAIL — `AttributeError: ... has no attribute 'list_my_shows'`.

- [ ] **Step 3: Implement the repository functions**

In `app/repositories/show.py`, add (ensure `from app.models.show import Show, ShowEntry, ShowStatus` is imported; `func` is already imported):

```python
_ACTIVE_STATUSES = (
    ShowStatus.registration_open,
    ShowStatus.registration_closed,
    ShowStatus.in_progress,
)
_PAST_STATUSES = (ShowStatus.completed, ShowStatus.cancelled)


def _my_shows_status_filter(status_group: str):
    if status_group == "active":
        return Show.status.in_(_ACTIVE_STATUSES)
    if status_group == "past":
        return Show.status.in_(_PAST_STATUSES)
    # all — обе группы (без draft)
    return Show.status.in_(_ACTIVE_STATUSES + _PAST_STATUSES)


async def list_my_shows(
    db: AsyncSession,
    user_id: uuid.UUID,
    status_group: str,
    *,
    page: int = 1,
    per_page: int = 12,
):
    """Выставки, где у пользователя есть запись, + число его записей.

    Возвращает список кортежей (Show, my_entries_count), пагинация по выставкам.
    """
    stmt = (
        select(Show, func.count(ShowEntry.id).label("cnt"))
        .join(ShowEntry, ShowEntry.show_id == Show.id)
        .where(
            ShowEntry.registered_by == user_id,
            _my_shows_status_filter(status_group),
        )
        .group_by(Show.id)
        .order_by(Show.date_start.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).all()


async def count_my_shows(
    db: AsyncSession, user_id: uuid.UUID, status_group: str
) -> int:
    """Число выставок (DISTINCT), где у пользователя есть запись, в группе."""
    stmt = (
        select(func.count(func.distinct(Show.id)))
        .select_from(Show)
        .join(ShowEntry, ShowEntry.show_id == Show.id)
        .where(
            ShowEntry.registered_by == user_id,
            _my_shows_status_filter(status_group),
        )
    )
    return int((await db.execute(stmt)).scalar_one())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/integration/test_my_shows.py::test_list_my_shows_groups_and_counts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repositories/show.py tests/integration/test_my_shows.py
git commit -m "feat(shows): repo aggregate list_my_shows + count_my_shows by status group"
```

---

## Task 4: Service — update_entry (PATCH logic)

**Files:**
- Modify: `app/services/show.py` (after `cancel_entry` ~line 478)
- Test: `tests/integration/test_my_shows.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_my_shows.py`:

```python
from app.services import show as svc


async def test_update_entry_changes_notes_and_keeps_catalog(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    user = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(breed_id=breed.id, name="Барс", sex=SexEnum.male,
              date_of_birth=date.today() - timedelta(days=600))
    db_session.add(dog)
    show = Show(organizer_id=user.id, name="Активная2", rank_id=rank.id,
                date_start=date.today(), status=ShowStatus.registration_open)
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
                      registered_by=user.id, catalog_number=5, notes="old")
    db_session.add(entry)
    await db_session.commit()

    updated = await svc.update_entry(
        db_session, show_id=show.id, entry_id=entry.id,
        requester_id=user.id, is_admin=False,
        show_class_id=None, handler_id=None, notes="new note",
        today=date.today(),
    )
    assert updated.notes == "new note"
    assert updated.catalog_number == 5  # сохранён


async def test_update_entry_forbidden_for_other_user(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    owner = await _user(db_session)
    other = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(breed_id=breed.id, name="Чужой", sex=SexEnum.male)
    db_session.add(dog)
    show = Show(organizer_id=owner.id, name="Активная3", rank_id=rank.id,
                date_start=date.today(), status=ShowStatus.registration_open)
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
                      registered_by=owner.id)
    db_session.add(entry)
    await db_session.commit()

    with pytest.raises(ValueError, match="forbidden"):
        await svc.update_entry(
            db_session, show_id=show.id, entry_id=entry.id,
            requester_id=other.id, is_admin=False,
            show_class_id=None, handler_id=None, notes="x",
            today=date.today(),
        )


async def test_update_entry_locked_when_registration_closed(db_session):
    breed = await _breed(db_session)
    rank = await _rank(db_session)
    user = await _user(db_session)
    cls = await _show_class(db_session, breed.animal_type_id)
    dog = Dog(breed_id=breed.id, name="Поздно", sex=SexEnum.male)
    db_session.add(dog)
    show = Show(organizer_id=user.id, name="Закрыта", rank_id=rank.id,
                date_start=date.today(), status=ShowStatus.registration_closed)
    db_session.add(show)
    await db_session.commit()
    entry = ShowEntry(show_id=show.id, dog_id=dog.id, show_class_id=cls.id,
                      registered_by=user.id)
    db_session.add(entry)
    await db_session.commit()

    with pytest.raises(ValueError, match="registration_locked"):
        await svc.update_entry(
            db_session, show_id=show.id, entry_id=entry.id,
            requester_id=user.id, is_admin=False,
            show_class_id=None, handler_id=None, notes="x",
            today=date.today(),
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/test_my_shows.py -k update_entry -v`
Expected: FAIL — `AttributeError: module 'app.services.show' has no attribute 'update_entry'`.

- [ ] **Step 3: Implement the service function**

In `app/services/show.py`, append after `cancel_entry` (reuse already-imported `repo`, `ShowClass`, `Breed`, `show_rules`, `ShowStatus`, `dog_repo`):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/test_my_shows.py -k update_entry -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/show.py tests/integration/test_my_shows.py
git commit -m "feat(shows): service update_entry (edit own entry while registration open)"
```

---

## Task 5: Router — wire the three endpoints

**Files:**
- Modify: `app/routers/shows.py` (Entries section, ~line 433-525)
- Test: `tests/integration/test_my_shows.py`

- [ ] **Step 1: Add the aggregate endpoint + change list_my_entries + add PATCH**

In `app/routers/shows.py`:

1. Import the new schemas at the top with the other schema imports:
```python
from app.schemas.show import (  # add to existing import block
    MyShowEntryResponse,
    MyShowItem,
    MyShowPage,
    ShowEntryUpdate,
)
```

2. Add the aggregate route in the Entries section, BEFORE `list_my_entries` (and therefore before `/{show_id}/...` routes). Note its path has no `{show_id}` so it cannot collide:
```python
@router.get(
    "/entries/my",
    response_model=MyShowPage,
    summary="Мои выставки (где у меня есть запись)",
)
async def list_my_shows(
    status_group: str = Query("all", pattern="^(all|active|past)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = await repo.list_my_shows(
        db, user.id, status_group, page=page, per_page=per_page
    )
    total = await repo.count_my_shows(db, user.id, status_group)
    items = [
        MyShowItem(
            **ShowResponse.model_validate(show).model_dump(),
            my_entries_count=cnt,
        )
        for show, cnt in rows
    ]
    return MyShowPage(items=items, total=total, page=page, per_page=per_page)
```
> If `ShowResponse` is not the name used elsewhere for the show output schema, match the one used by `GET /shows/{show_id}` (grep `response_model=` near the show detail route). `MyShowItem` subclasses it, so the dump/spread keeps all show fields.

3. Replace the body of the existing `list_my_entries` (currently `return await repo.list_user_entries_for_show(...)`) to return enriched rows, and set `response_model=list[MyShowEntryResponse]`:
```python
@router.get(
    "/{show_id}/entries/my",
    response_model=list[MyShowEntryResponse],
    summary="Мои записи на эту выставку",
)
async def list_my_entries(
    show_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = await repo.list_user_entries_for_show_enriched(db, show_id, user.id)
    return [
        MyShowEntryResponse(
            **ShowEntryResponse.model_validate(entry).model_dump(),
            dog_name=dog_name,
            class_code=class_code,
            class_name=class_name,
        )
        for entry, dog_name, class_code, class_name in rows
    ]
```

4. Add the PATCH route after `cancel_entry`:
```python
@router.patch(
    "/{show_id}/entries/{entry_id}",
    response_model=MyShowEntryResponse,
    summary="Изменить свою запись",
)
async def update_entry(
    show_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: ShowEntryUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        await svc.update_entry(
            db,
            show_id=show_id,
            entry_id=entry_id,
            requester_id=user.id,
            is_admin=_is_admin(user),
            show_class_id=body.show_class_id,
            handler_id=body.handler_id,
            notes=body.notes,
            today=date.today(),
        )
    except ValueError as e:
        _raise_for_error(e)
    row = await repo.get_entry_enriched(db, entry_id)
    entry, dog_name, class_code, class_name = row
    return MyShowEntryResponse(
        **ShowEntryResponse.model_validate(entry).model_dump(),
        dog_name=dog_name,
        class_code=class_code,
        class_name=class_name,
    )
```

- [ ] **Step 2: Write an integration test through the OpenAPI surface**

Add to `tests/integration/test_my_shows.py` (validates routing + schema only; auth-dependent flows are covered at service/repo level above):

```python
async def test_my_shows_route_registered_in_openapi(client):
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/shows/entries/my" in paths
    assert "get" in paths["/shows/entries/my"]
    assert "patch" in paths["/shows/{show_id}/entries/{entry_id}"]
```

- [ ] **Step 3: Run the suite**

Run: `pytest tests/integration/test_my_shows.py -v`
Expected: all PASS.

- [ ] **Step 4: Full gate**

Run: `pytest -q` (or the project's configured test command from `pyproject.toml`).
Expected: no new failures. If the repo enforces lint/type gates (`ruff`, `pyright`/`mypy` per `pyproject.toml`), run them and fix to zero issues.

- [ ] **Step 5: Commit**

```bash
git add app/routers/shows.py tests/integration/test_my_shows.py
git commit -m "feat(shows): GET /shows/entries/my, enriched entries/my, PATCH entry"
```

---

## Self-Review Checklist (run after all tasks)

- [ ] `GET /shows/entries/my?status_group=active|past|all&page&per_page` returns `{items: MyShowItem[], total, page, per_page}`; `MyShowItem` includes all show fields + `my_entries_count`. (Spec §Бэкенд.1)
- [ ] `GET /shows/{id}/entries/my` items include `dog_name`, `class_code`, `class_name`. (Spec §Бэкенд.2)
- [ ] `PATCH /shows/{id}/entries/{entry_id}` edits class/handler/notes, keeps `catalog_number`, rejects non-owner (403) and closed registration (422). (Spec §Бэкенд.3)
- [ ] All endpoints scope to `get_current_user`; no cross-user data exposure. (Spec §Decisions.5)
- [ ] Existing `/shows/{id}/entries` (catalog) and `POST entries` still use plain `ShowEntryResponse` — unchanged.

## Hand-off

The frontend plan (`show-ring-frontend/docs/superpowers/plans/2026-06-09-my-shows-frontend.md`) consumes this contract. Run the backend with `:8000/health/` green before frontend runtime verification.
