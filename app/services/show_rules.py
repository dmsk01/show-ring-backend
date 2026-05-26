"""
Правила РКФ для выставок (этап 6).

Изолирует "доменные знания" о правилах выставок от CRUD-логики:
- вычисление возраста собаки на дату выставки;
- список доступных классов по возрасту;
- классы, требующие документов (рабочий, чемпионы);
- валидация переходов состояния выставки.

Зачем отдельный модуль: правила РКФ меняются (новые редакции положения),
и держать их в одном месте проще для аудита/обновления, чем рассыпать
по сервисам.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference import Grade, ShowClass, ShowRank, Title
from app.models.show import ShowStatus


# ---------------------------------------------------------------------
# Возраст и классы
# ---------------------------------------------------------------------


# Коды классов, которые требуют дополнительных документов. На этапе 6
# не валидируем строго (нет таблицы сертификатов/титулов собаки), но
# помечаем в ответе — фронт показывает предупреждение.
WORKING_CLASS_CODE = "working"
CHAMPIONS_CLASS_CODE = "champions"

REQUIRES_DOCS_NOTES = {
    WORKING_CLASS_CODE: "Требуется рабочий сертификат",
    CHAMPIONS_CLASS_CODE: "Требуется титул Чемпион",
}


def age_in_months_on(date_of_birth: date, on_date: date) -> int:
    """
    Возраст в полных месяцах на конкретную дату.

    Для классов РКФ важна именно дата выставки (а не сегодняшняя):
    собака может быть записана в юниоров, если ей <18 мес на день
    показа, даже если за неделю до выставки ей исполняется 18.

    Формула: (год2-год1)*12 + (мес2-мес1), скорректировано вниз, если
    день2 < день1 (ещё не отметили "месяц").
    """
    if on_date < date_of_birth:
        return 0
    months = (on_date.year - date_of_birth.year) * 12 + (
        on_date.month - date_of_birth.month
    )
    if on_date.day < date_of_birth.day:
        months -= 1
    return max(months, 0)


@dataclass
class AvailableClassInfo:
    """
    Описание одного класса как кандидата для записи. Чистая dataclass —
    в роутере конвертируется в pydantic-схему AvailableClass.
    """

    id: uuid.UUID
    code: str
    name: str
    age_from_months: int
    age_to_months: int | None
    can_receive_cac: bool
    requires_documents: bool
    documents_note: str | None


def _class_matches_age(cls: ShowClass, age_months: int) -> bool:
    """Класс подходит по возрасту?"""
    if age_months < cls.age_from_months:
        return False
    if cls.age_to_months is not None and age_months > cls.age_to_months:
        return False
    return True


async def list_available_classes_for_age(
    db: AsyncSession,
    animal_type_id: uuid.UUID,
    age_months: int,
) -> list[AvailableClassInfo]:
    """
    Возвращает все классы выставки, в которые собака с указанным
    возрастом может быть записана.

    Возраст границы — включительные с обеих сторон (РКФ-логика:
    "от 6 до 9 месяцев включительно"). Конкретный класс выбирает
    владелец, а не система.

    age_to_months=None означает "без верхней границы" — открытый класс,
    рабочий класс, чемпионов, ветераны.
    """
    stmt = select(ShowClass).where(ShowClass.animal_type_id == animal_type_id)
    rows = (await db.execute(stmt)).scalars().all()

    result: list[AvailableClassInfo] = []
    for cls in rows:
        if not _class_matches_age(cls, age_months):
            continue
        needs_docs = cls.code in REQUIRES_DOCS_NOTES
        result.append(
            AvailableClassInfo(
                id=cls.id,
                code=cls.code,
                name=cls.name,
                age_from_months=cls.age_from_months,
                age_to_months=cls.age_to_months,
                can_receive_cac=cls.can_receive_cac,
                requires_documents=needs_docs,
                documents_note=REQUIRES_DOCS_NOTES.get(cls.code),
            )
        )
    # Сортируем по возрасту "вход" — фронту удобнее показывать "бэби,
    # щенки, юниоры…" в естественном порядке.
    result.sort(key=lambda c: c.age_from_months)
    return result


# ---------------------------------------------------------------------
# Статусная машина выставки
# ---------------------------------------------------------------------


# Разрешённые переходы. Ключ — текущее состояние, значение — куда можно.
# cancelled — терминальное (нельзя восстановить выставку, нужна новая).
# completed — терминальное.
ALLOWED_TRANSITIONS: dict[ShowStatus, set[ShowStatus]] = {
    ShowStatus.draft: {
        ShowStatus.registration_open,
        ShowStatus.cancelled,
    },
    ShowStatus.registration_open: {
        ShowStatus.registration_closed,
        ShowStatus.cancelled,
    },
    ShowStatus.registration_closed: {
        ShowStatus.in_progress,
        ShowStatus.cancelled,
    },
    ShowStatus.in_progress: {
        ShowStatus.completed,
        ShowStatus.cancelled,
    },
    ShowStatus.completed: set(),
    ShowStatus.cancelled: set(),
}


def is_transition_allowed(
    current: ShowStatus, target: ShowStatus
) -> bool:
    """Можно ли перейти из current в target по правилам РКФ-цикла."""
    return target in ALLOWED_TRANSITIONS.get(current, set())


# ---------------------------------------------------------------------
# Правила присвоения титулов
# ---------------------------------------------------------------------


# Коды титулов из справочника (см. seed_references.TITLES).
TITLE_CW = "cw"
TITLE_CAC = "cac"
TITLE_R_CAC = "r-cac"
TITLE_CACIB = "cacib"
TITLE_R_CACIB = "r-cacib"
TITLE_BOB = "bob"
TITLE_BIG = "big"
TITLE_BIS = "bis"
TITLE_JUW = "juw"   # ЮСАС — юный кандидат в чемпионы

# Коды оценок.
GRADE_EXCELLENT = "excellent"

# Коды классов.
CLASS_JUNIOR = "junior"
CLASS_BABY = "baby"
CLASS_PUPPY = "puppy"
CLASS_VETERAN = "veteran"
# Для CACIB-выставок особый код ранга (см. seed_references.SHOW_RANKS).
RANK_CACIB = "cacib"


@dataclass
class TitleAward:
    """
    Описание одного присуждённого титула. title_id нужен для INSERT'а
    в dog_titles; code/name удобны для кэширования в titles_cache.
    """

    title_id: uuid.UUID
    code: str
    name: str


async def _load_titles_by_code(
    db: AsyncSession, animal_type_id: uuid.UUID, codes: list[str]
) -> dict[str, Title]:
    """
    Подтягивает Title-записи по списку кодов в рамках animal_type.
    Возвращает map code → Title. Если код не найден — он просто не
    попадёт в результат (например, "cacib" на этапе seed не заведён —
    тогда титул не присваивается).
    """
    if not codes:
        return {}
    stmt = select(Title).where(
        Title.animal_type_id == animal_type_id,
        Title.code.in_(codes),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {t.code: t for t in rows}


def _is_cac_eligible_class(cls: ShowClass) -> bool:
    """
    Класс может выдавать CAC, если в справочнике стоит флаг
    can_receive_cac. Семантически это intermediate/open/working/champions.
    """
    return cls.can_receive_cac


async def compute_class_titles(
    db: AsyncSession,
    *,
    animal_type_id: uuid.UUID,
    show_class: ShowClass,
    show_rank: ShowRank,
    grade: Grade | None,
    placement: int | None,
) -> list[TitleAward]:
    """
    Определяет, какие титулы получает собака за этот результат **в ринге
    класса** (без учёта общепородных уровней BOB/BIG/BIS — те выдаются
    отдельным API-вызовом).

    Логика РКФ:
    - grade=excellent + placement=1 → CW (победитель класса)
      - CW в can_receive_cac классе → CAC
      - CW в classе юниоров → ЮСАС
    - grade=excellent + placement=2 в can_receive_cac → R.CAC
    - Дисквалификация или отсутствие оценки → ничего.

    Названия CACIB/R.CACIB присваиваются на уровне BOB (best-of-breed)
    для CACIB-выставок, не здесь.
    """
    if grade is None or grade.is_disqualifying:
        return []

    is_excellent = grade.code == GRADE_EXCELLENT
    award_codes: list[str] = []

    if placement == 1 and is_excellent:
        award_codes.append(TITLE_CW)
        if _is_cac_eligible_class(show_class):
            award_codes.append(TITLE_CAC)
        if show_class.code == CLASS_JUNIOR:
            award_codes.append(TITLE_JUW)
    elif placement == 2 and is_excellent and _is_cac_eligible_class(show_class):
        award_codes.append(TITLE_R_CAC)

    if not award_codes:
        return []

    titles_map = await _load_titles_by_code(db, animal_type_id, award_codes)
    # Если каких-то кодов в справочнике нет — мягко игнорируем,
    # вместо падения 500-кой. Например, на ранке без CAC можно
    # отключить выдачу CAC, удалив его из titles. Логика выше всё равно
    # его "вычислит", а словарь просто его не вернёт.
    return [
        TitleAward(title_id=t.id, code=t.code, name=t.name)
        for c in award_codes
        if (t := titles_map.get(c)) is not None
    ]


def is_cacib_rank(show_rank: ShowRank) -> bool:
    """CACIB-выставка ли — отдельный признак для best-of-breed логики."""
    return show_rank.code == RANK_CACIB


async def get_best_of_breed_titles(
    db: AsyncSession,
    *,
    animal_type_id: uuid.UUID,
    show_rank: ShowRank,
    is_bob: bool,
    is_best_male: bool,
    is_best_female: bool,
) -> list[TitleAward]:
    """
    Титулы за BOB/ЛК/ЛС. На CACIB — ещё и CACIB / R.CACIB.

    Логика:
    - ЛК (best_male) → BOB-кандидат + (на CACIB) → CACIB
    - ЛС (best_female) → BOB-кандидат + (на CACIB) → CACIB (вторая собака)
    - is_bob=True → BOB

    Упрощения этапа 7:
    - CACIB присуждается ЛК и ЛС (двум). Полная логика РКФ выдаёт CACIB
      одной + R.CACIB второй; на этом этапе считаем ЛК → CACIB, ЛС → CACIB
      (фактически это формирование шорт-листа для FCI). R.CACIB можно
      присуждать вручную через POST /results/{rid} при корректировке.
    """
    codes: list[str] = []
    if is_bob:
        codes.append(TITLE_BOB)
    if is_cacib_rank(show_rank) and (is_best_male or is_best_female):
        codes.append(TITLE_CACIB)

    titles_map = await _load_titles_by_code(db, animal_type_id, codes)
    return [
        TitleAward(title_id=t.id, code=t.code, name=t.name)
        for c in codes
        if (t := titles_map.get(c)) is not None
    ]


async def get_big_title(
    db: AsyncSession, animal_type_id: uuid.UUID
) -> TitleAward | None:
    """Один титул BIG."""
    titles_map = await _load_titles_by_code(db, animal_type_id, [TITLE_BIG])
    t = titles_map.get(TITLE_BIG)
    return TitleAward(title_id=t.id, code=t.code, name=t.name) if t else None


async def get_bis_title(
    db: AsyncSession, animal_type_id: uuid.UUID
) -> TitleAward | None:
    """Один титул BIS."""
    titles_map = await _load_titles_by_code(db, animal_type_id, [TITLE_BIS])
    t = titles_map.get(TITLE_BIS)
    return TitleAward(title_id=t.id, code=t.code, name=t.name) if t else None
