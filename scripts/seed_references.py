"""
Seed-скрипт для справочников (этап 3).

Запуск:
    python scripts/seed_references.py

Что делает:
1. Создаёт animal_type "dog", если ещё нет.
2. Создаёт 10 FCI-групп для собак.
3. Создаёт начальный набор популярных пород (с привязкой к группам и
   номерами FCI).
4. Создаёт 8 выставочных классов РКФ.
5. Создаёт ранги, титулы и оценки.

Идемпотентность:
- Каждый INSERT обёрнут проверкой "существует ли запись с таким кодом".
- Можно безопасно запускать многократно — данные не дублируются.
- Английские переводы (name_en/description_en) дозаполняются и для УЖЕ
  существующих записей, если перевод пуст (upsert перевода) — повторный
  запуск на засеянной базе локализует её без пересоздания данных.

Это не Alembic data migration, потому что справочники могут пополняться
со временем (новые породы FCI). data migration лучше работает для
одноразовых изменений; seed-скрипт удобнее для регулярных обновлений.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path, чтобы скрипт работал и из
# E:/.../scripts/seed_references.py, и из E:/.../python scripts/...
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, engine
from app.models.reference import (
    AnimalType,
    Breed,
    BreedGroup,
    Grade,
    ShowClass,
    ShowRank,
    Title,
)
# Полный справочник пород FCI вынесен в data-модуль. Это длинный список
# (200+ строк), который имеет смысл хранить отдельно от логики seed-скрипта:
# легче поддерживать, не мешает читать саму процедуру наполнения.
from scripts.data.fci_breeds import FCI_BREEDS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed")


# ---------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------


# (number, code, name_ru, name_en) — name_en по номенклатуре FCI.
FCI_GROUPS = [
    (1, "fci-1", "Овчарки и скотогонные (кроме швейцарских)",
     "Sheepdogs and Cattledogs (except Swiss Cattledogs)"),
    (2, "fci-2", "Пинчеры и шнауцеры, молоссы и швейцарские пастушьи",
     "Pinscher and Schnauzer - Molossoid and Swiss Mountain and Cattledogs"),
    (3, "fci-3", "Терьеры", "Terriers"),
    (4, "fci-4", "Таксы", "Dachshunds"),
    (5, "fci-5", "Шпицы и примитивные", "Spitz and Primitive Types"),
    (6, "fci-6", "Гончие и ищейки", "Scent Hounds and Related Breeds"),
    (7, "fci-7", "Легавые", "Pointing Dogs"),
    (8, "fci-8", "Ретриверы, спаниели, водяные собаки",
     "Retrievers - Flushing Dogs - Water Dogs"),
    (9, "fci-9", "Декоративные и собаки-компаньоны", "Companion and Toy Dogs"),
    (10, "fci-10", "Борзые", "Sighthounds"),
]


# BREEDS_SEED перенесён в scripts/data/fci_breeds.py — импортируется как
# FCI_BREEDS. Формат тот же: (group_number, code, name_ru, name_en, fci_number).
BREEDS_SEED = FCI_BREEDS


# По регламенту РКФ (Положение о сертификатных выставках).
# (code, name_ru, name_en, age_from, age_to, can_receive_cac)
# age_to=None означает "верхняя граница не задана".
# can_receive_cac — может ли в этом классе быть выдан CAC/CACIB.
SHOW_CLASSES = [
    ("baby", "Класс бэби", "Baby Class", 4, 6, False),
    ("puppy", "Класс щенков", "Puppy Class", 6, 9, False),
    ("junior", "Класс юниоров", "Junior Class", 9, 18, False),
    ("intermediate", "Промежуточный класс", "Intermediate Class", 15, 24, True),
    ("open", "Открытый класс", "Open Class", 15, None, True),
    ("working", "Рабочий класс", "Working Class", 15, None, True),
    ("champions", "Класс чемпионов", "Champion Class", 15, None, True),
    ("veteran", "Класс ветеранов", "Veteran Class", 96, None, False),
]


# (code, name_ru, name_en, description_ru, description_en)
SHOW_RANKS = [
    ("cacib", "CACIB (международная)", "CACIB (International)",
     "FCI-выставка с правом присвоения CACIB",
     "FCI show entitled to award the CACIB"),
    ("cac-chrkf-os", "ЧРКФ ОС", "RKF Championship (FCI Group)",
     "Чемпионат РКФ по группе FCI",
     "RKF Championship for an FCI breed group"),
    ("cac-chrkf", "ЧРКФ", "RKF Championship",
     "Чемпионат РКФ", "Championship of the Russian Kynological Federation"),
    ("cac-chf", "ЧФ", "Federation Championship",
     "Чемпионат федерации", "Championship of an RKF member federation"),
    ("kchk", "КЧК", "Club Champion Candidate",
     "Кандидат в чемпионы клуба", "Candidate for Club Champion"),
    ("pk", "ПК", "Club Winner",
     "Победитель клуба", "Club Winner"),
    ("chk", "ЧК", "Club Champion",
     "Чемпион клуба", "Club Champion"),
    ("monoporodnaya", "Монопородная", "Single-Breed (Specialty)",
     "Монопородная выставка", "Single-breed (specialty) show"),
]


# (code, name_ru, name_en, is_reserve)
TITLES = [
    ("cw", "CW (Class Winner)", "CW (Class Winner)", False),
    ("cac", "CAC", "CAC", False),
    ("r-cac", "R.CAC (резервный CAC)", "R.CAC (Reserve CAC)", True),
    ("cacib", "CACIB", "CACIB", False),
    ("r-cacib", "R.CACIB (резервный CACIB)", "R.CACIB (Reserve CACIB)", True),
    ("bob", "BOB (Best of Breed) — Лучший представитель породы",
     "BOB (Best of Breed)", False),
    ("bos", "BOS (Best of Opposite Sex) — Лучший противоположного пола",
     "BOS (Best of Opposite Sex)", False),
    ("big", "BIG (Best in Group)", "BIG (Best in Group)", False),
    ("r-big", "R.BIG (резервный BIG)", "R.BIG (Reserve BIG)", True),
    ("bis", "BIS (Best in Show)", "BIS (Best in Show)", False),
    ("r-bis", "R.BIS (резервный BIS)", "R.BIS (Reserve BIS)", True),
    ("juw", "ЮСАС (юный кандидат)", "JCAC (Junior CAC)", False),
    ("vw", "Ветеран-победитель класса", "Veteran Class Winner", False),
]


# (code, name_ru, name_en, is_disqualifying, is_puppy_grade)
GRADES = [
    ("excellent", "Отлично", "Excellent", False, False),
    ("very-good", "Очень хорошо", "Very Good", False, False),
    ("good", "Хорошо", "Good", False, False),
    ("satisfactory", "Удовлетворительно", "Satisfactory", False, False),
    ("disqualification", "Дисквалификация", "Disqualification", True, False),
    ("absent", "Отсутствует", "Absent", False, False),
    # Щенячьи оценки — отдельные.
    ("great-promise", "Большая перспектива", "Very Promising", False, True),
    ("promising", "Перспективный", "Promising", False, True),
    ("less-promising", "Малоперспективный", "Less Promising", False, True),
    ("not-promising", "Неперспективный", "Not Promising", False, True),
]


# ---------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------


async def _get_or_create(
    db: AsyncSession,
    model,
    lookup: dict,
    create_fields: dict,
    backfill: dict | None = None,
):
    """
    Идемпотентный upsert по lookup-полям. Если запись есть — возвращает
    её, если нет — создаёт. Не делаем on_conflict_do_nothing, чтобы
    логика оставалась прозрачной и работала на любом диалекте.

    backfill — поля, которые дозаполняются у СУЩЕСТВУЮЩЕЙ записи, если
    там сейчас NULL (используется для name_en/description_en: база,
    засеянная до локализации, получает переводы повторным запуском).
    Непустые значения не перезаписываем — правки из админки важнее сида.
    """
    stmt = select(model)
    for k, v in lookup.items():
        stmt = stmt.where(getattr(model, k) == v)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        changed = False
        for k, v in (backfill or {}).items():
            if v is not None and getattr(existing, k) is None:
                setattr(existing, k, v)
                changed = True
        if changed:
            await db.flush()
        return existing, False
    obj = model(**create_fields)
    db.add(obj)
    await db.flush()
    return obj, True


# ---------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------


async def seed(db: AsyncSession) -> None:
    # 1. animal_type=dog
    dog, created = await _get_or_create(
        db,
        AnimalType,
        lookup={"code": "dog"},
        create_fields={"code": "dog", "name": "Собака", "name_en": "Dog"},
        backfill={"name_en": "Dog"},
    )
    logger.info("animal_type 'dog': %s", "created" if created else "exists")

    # 2. Группы FCI
    group_by_number: dict[int, BreedGroup] = {}
    for number, code, name, name_en in FCI_GROUPS:
        grp, created = await _get_or_create(
            db,
            BreedGroup,
            lookup={"animal_type_id": dog.id, "number": number},
            create_fields={
                "animal_type_id": dog.id,
                "number": number,
                "code": code,
                "name": name,
                "name_en": name_en,
            },
            backfill={"name_en": name_en},
        )
        group_by_number[number] = grp
        if created:
            logger.info("breed_group %s: created", code)

    # 3. Породы
    breeds_created = 0
    for group_num, code, name, name_en, fci_no in BREEDS_SEED:
        grp = group_by_number.get(group_num)
        _, created = await _get_or_create(
            db,
            Breed,
            lookup={"animal_type_id": dog.id, "code": code},
            create_fields={
                "animal_type_id": dog.id,
                "breed_group_id": grp.id if grp else None,
                "code": code,
                "name": name,
                "name_en": name_en,
                "fci_number": fci_no,
            },
            backfill={"name_en": name_en},
        )
        if created:
            breeds_created += 1
    logger.info("breeds: %d created", breeds_created)

    # 4. Выставочные классы
    classes_created = 0
    for code, name, name_en, age_from, age_to, can_cac in SHOW_CLASSES:
        _, created = await _get_or_create(
            db,
            ShowClass,
            lookup={"animal_type_id": dog.id, "code": code},
            create_fields={
                "animal_type_id": dog.id,
                "code": code,
                "name": name,
                "name_en": name_en,
                "age_from_months": age_from,
                "age_to_months": age_to,
                "can_receive_cac": can_cac,
            },
            backfill={"name_en": name_en},
        )
        if created:
            classes_created += 1
    logger.info("show_classes: %d created", classes_created)

    # 5. Ранги
    ranks_created = 0
    for code, name, name_en, descr, descr_en in SHOW_RANKS:
        _, created = await _get_or_create(
            db,
            ShowRank,
            lookup={"code": code},
            create_fields={
                "code": code,
                "name": name,
                "name_en": name_en,
                "description": descr,
                "description_en": descr_en,
            },
            backfill={"name_en": name_en, "description_en": descr_en},
        )
        if created:
            ranks_created += 1
    logger.info("show_ranks: %d created", ranks_created)

    # 6. Титулы
    titles_created = 0
    for code, name, name_en, is_reserve in TITLES:
        _, created = await _get_or_create(
            db,
            Title,
            lookup={"animal_type_id": dog.id, "code": code},
            create_fields={
                "animal_type_id": dog.id,
                "code": code,
                "name": name,
                "name_en": name_en,
                "is_reserve": is_reserve,
            },
            backfill={"name_en": name_en},
        )
        if created:
            titles_created += 1
    logger.info("titles: %d created", titles_created)

    # 7. Оценки
    grades_created = 0
    for code, name, name_en, is_disq, is_puppy in GRADES:
        _, created = await _get_or_create(
            db,
            Grade,
            lookup={"animal_type_id": dog.id, "code": code},
            create_fields={
                "animal_type_id": dog.id,
                "code": code,
                "name": name,
                "name_en": name_en,
                "is_disqualifying": is_disq,
                "is_puppy_grade": is_puppy,
            },
            backfill={"name_en": name_en},
        )
        if created:
            grades_created += 1
    logger.info("grades: %d created", grades_created)

    await db.commit()


async def main() -> None:
    try:
        async with async_session_factory() as db:
            await seed(db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
