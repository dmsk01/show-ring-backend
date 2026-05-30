# app/services/document_official.py
"""
Билдеры контекста для официальных документов РКФ (docxtpl).

Разделение: чистые `_shape_*` собирают словарь из простых значений
(тестируются без БД); `build_*_context` грузят ORM и зовут шейпер.

Имена людей резолвятся через app.utils.names (profile должен быть
подгружен заранее).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog
from app.models.kennel import Kennel
from app.models.reference import Breed, BreedGroup, Grade, ShowClass, ShowRank
from app.models.result import ShowResult
from app.models.show import Show, ShowEntry, ShowJudge, ShowRing
from app.models.user import User
from app.utils.names import full_name, judge_display


def _fmt_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _s(value: object | None) -> str:
    return str(value) if value not in (None, "") else ""


# ---------------------------------------------------------------------
# Резолвер имён с профилем (async)
# ---------------------------------------------------------------------


async def _load_user_with_profile(
    db: AsyncSession, user_id: uuid.UUID | None
) -> User | None:
    if user_id is None:
        return None
    user = await db.get(User, user_id)
    if user is not None:
        # AsyncAttrs (Base) — ленивую связь profile грузим явно.
        await user.awaitable_attrs.profile
    return user


# ---------------------------------------------------------------------
# Диплом
# ---------------------------------------------------------------------


@dataclass
class DiplomaInput:
    show_name: str
    show_rank: str | None
    judge: str | None
    breed: str
    sex: str  # "male" | "female"
    class_name: str
    grade: str | None
    title: str | None
    placement: int | None
    dog_name: str
    tattoo: str | None
    microchip: str | None
    date_of_birth: date | None
    owner: str | None
    kennel: str | None
    breeder: str | None
    pedigree: str | None
    catalog_number: int | None = None
    fci_number: str | None = None


# Пол словом — реальный бланк диплома РКФ печатает «Кобель»/«Сука», а не
# галочку. sex_male/sex_female оставлены для шаблонов с отметкой-галочкой.
_SEX_WORD = {"male": "Кобель", "female": "Сука"}


def _shape_diploma_context(data: DiplomaInput) -> dict:
    # Строка породы в бланке: «(FCI 327) НАЗВАНИЕ». Если номера FCI нет —
    # только название.
    breed_line = _s(data.breed)
    if data.fci_number:
        breed_line = f"(FCI {data.fci_number}) {breed_line}".strip()
    return {
        "show_name": _s(data.show_name),
        "show_rank": _s(data.show_rank),
        "judge": _s(data.judge),
        "breed": _s(data.breed),
        "fci_number": _s(data.fci_number),
        "breed_line": breed_line,
        "sex_male": data.sex == "male",
        "sex_female": data.sex == "female",
        "sex_word": _SEX_WORD.get(data.sex, ""),
        "class_name": _s(data.class_name),
        "grade": _s(data.grade),
        "title": _s(data.title),
        "place": _s(data.placement),
        "catalog_number": _s(data.catalog_number),
        "dog_name": _s(data.dog_name),
        "tattoo": _s(data.tattoo),
        "microchip": _s(data.microchip),
        "dob": _fmt_date(data.date_of_birth),
        "owner": _s(data.owner),
        "kennel": _s(data.kennel),
        "breeder": _s(data.breeder),
        "pedigree": _s(data.pedigree),
    }


async def _resolve_breeder(
    db: AsyncSession, dog: Dog
) -> tuple[str, str]:
    """Возвращает (breeder_name, breeder_kennel_prefix)."""
    if dog.breeder_kennel_id is not None:
        kennel = await db.get(Kennel, dog.breeder_kennel_id)
        if kennel is not None:
            owner = await _load_user_with_profile(db, kennel.owner_id)
            return full_name(owner), _s(kennel.kennel_prefix or kennel.name)
    return _s(dog.breeder_name), ""


async def _resolve_owner(db: AsyncSession, dog: Dog) -> str:
    if dog.kennel_id is not None:
        kennel = await db.get(Kennel, dog.kennel_id)
        if kennel is not None:
            owner = await _load_user_with_profile(db, kennel.owner_id)
            return full_name(owner)
    return ""


async def build_diploma_context(
    db: AsyncSession, entry_id: uuid.UUID
) -> dict:
    entry = await db.get(ShowEntry, entry_id)
    if entry is None:
        raise ValueError("entry_not_found")
    show = await db.get(Show, entry.show_id)
    if show is None:
        raise ValueError("not_found")
    rank = await db.get(ShowRank, show.rank_id)
    dog = await db.get(Dog, entry.dog_id)
    if dog is None:
        raise ValueError("dog_not_found")
    breed = await db.get(Breed, dog.breed_id)
    cls = await db.get(ShowClass, entry.show_class_id)

    result = (
        await db.execute(
            select(ShowResult).where(ShowResult.show_entry_id == entry_id)
        )
    ).scalar_one_or_none()

    grade_name = None
    judge = None
    title = None
    if result is not None:
        if result.grade_id is not None:
            grade = await db.get(Grade, result.grade_id)
            grade_name = grade.name if grade else None
        judge_user = await _load_user_with_profile(db, result.judge_id)
        judge = judge_display(judge_user) if judge_user else None
        titles = [
            t.get("name", t.get("code", ""))
            for t in (result.titles_cache or [])
        ]
        title = ", ".join(t for t in titles if t) or None

    owner = await _resolve_owner(db, dog)
    breeder, kennel_prefix = await _resolve_breeder(db, dog)

    return _shape_diploma_context(
        DiplomaInput(
            show_name=show.name,
            show_rank=rank.name if rank else None,
            judge=judge,
            breed=breed.name if breed else "",
            sex=dog.sex.value,
            class_name=cls.name if cls else "",
            grade=grade_name,
            title=title,
            placement=result.placement if result else None,
            dog_name=dog.name,
            tattoo=dog.tattoo,
            microchip=dog.microchip,
            date_of_birth=dog.date_of_birth,
            owner=owner,
            kennel=kennel_prefix,
            breeder=breeder,
            pedigree=dog.rkf_number,
            catalog_number=entry.catalog_number,
            fci_number=breed.fci_number if breed else None,
        )
    )


async def build_diplomas_batch_context(
    db: AsyncSession, show_id: uuid.UUID
) -> dict:
    """Контекст для одного файла со всеми дипломами выставки.

    Каждый диплом — это словарь от build_diploma_context. Битые записи
    (нет собаки/результата) пропускаем, не валя всю пачку.
    """
    entry_ids = (
        await db.execute(
            select(ShowEntry.id).where(ShowEntry.show_id == show_id)
        )
    ).scalars().all()
    diplomas: list[dict] = []
    for eid in entry_ids:
        try:
            diplomas.append(await build_diploma_context(db, eid))
        except ValueError:
            continue
    return {"diplomas": diplomas}


# ---------------------------------------------------------------------
# Ринговая ведомость
# ---------------------------------------------------------------------


_SEX_RU = {"male": "кобели", "female": "суки"}

# Месяцы в родительном падеже для длинной даты бланка («22 ноября 2025 г.»).
_MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _fmt_date_long(value: date | None) -> str:
    """«22 ноября 2025 г.» — формат даты в шапке ринговой ведомости."""
    if value is None:
        return ""
    return f"{value.day} {_MONTHS_RU[value.month - 1]} {value.year} г."


@dataclass
class RingSheetInput:
    """
    Один бланк ринговой ведомости — НА ПОРОДУ (как в образце РКФ): шапка
    (организатор / выставка / порода / судья / дата / ринг) + список
    номеров по каталогу собак этой породы. Оценки и титулы судья
    заполняет от руки, поэтому в контекст не входят.
    """

    organizer: str | None
    show_title: str  # название + ранг выставки
    breed: str
    judge: str | None
    date: str  # уже форматированная (длинная) дата
    ring_number: int | None
    catalog_numbers: list[int | None]


def _shape_ring_sheet(data: RingSheetInput) -> dict:
    return {
        "organizer": _s(data.organizer),
        "show_title": _s(data.show_title),
        "breed": _s(data.breed),
        "judge": _s(data.judge),
        "date": _s(data.date),
        "ring_number": _s(data.ring_number),
        # Номера по каталогу: и списком (для цикла), и строкой (для
        # вставки «через запятую», если в шаблоне нет сетки-цикла).
        "numbers": [_s(n) for n in data.catalog_numbers],
        "numbers_str": ", ".join(_s(n) for n in data.catalog_numbers if _s(n)),
    }


async def build_ring_sheets_context(
    db: AsyncSession,
    show_id: uuid.UUID,
    ring_id: uuid.UUID | None = None,
) -> dict:
    """
    Контекст файла с ринговыми ведомостями. Один бланк = одна порода
    (как в образце РКФ): шапка + список номеров по каталогу.

    Группировка: записи выставки → по породе собаки. Для каждой породы
    шапка берёт назначенного судью и номер ринга из ShowRing (если есть),
    дату — из ShowRing.ring_date или show.date_start.

    ring_id оставлен для совместимости сигнатуры: если задан, ограничиваем
    одним рингом (его породой).
    """
    show = await db.get(Show, show_id)
    if show is None:
        raise ValueError("not_found")
    rank = await db.get(ShowRank, show.rank_id)
    show_title = show.name + (f" ранга {rank.name}" if rank else "")

    # Назначения по рингам: breed_id → (ring_number, judge_id, ring_date).
    rings = (
        await db.execute(select(ShowRing).where(ShowRing.show_id == show_id))
    ).scalars().all()
    ring_by_breed: dict[uuid.UUID, ShowRing] = {
        r.breed_id: r for r in rings if r.breed_id is not None
    }
    # Запасной источник судьи — назначения ShowJudge на породу.
    show_judges = (
        await db.execute(select(ShowJudge).where(ShowJudge.show_id == show_id))
    ).scalars().all()
    judge_id_by_breed: dict[uuid.UUID, uuid.UUID] = {
        j.breed_id: j.judge_id for j in show_judges if j.breed_id is not None
    }

    only_breed_id: uuid.UUID | None = None
    if ring_id is not None:
        ring = await db.get(ShowRing, ring_id)
        only_breed_id = ring.breed_id if ring is not None else None

    # Группируем номера по каталогу по породе собаки.
    entries = (
        await db.execute(
            select(ShowEntry)
            .where(ShowEntry.show_id == show_id)
            .order_by(ShowEntry.catalog_number.asc().nullslast())
        )
    ).scalars().all()
    numbers_by_breed: dict[uuid.UUID, list[int | None]] = {}
    breed_obj: dict[uuid.UUID, Breed] = {}
    for e in entries:
        dog = await db.get(Dog, e.dog_id)
        if dog is None:
            continue
        if only_breed_id is not None and dog.breed_id != only_breed_id:
            continue
        if dog.breed_id not in breed_obj:
            br = await db.get(Breed, dog.breed_id)
            if br is None:
                continue
            breed_obj[dog.breed_id] = br
        numbers_by_breed.setdefault(dog.breed_id, []).append(e.catalog_number)

    sheets: list[dict] = []
    for breed_id in sorted(numbers_by_breed, key=lambda b: breed_obj[b].name):
        breed = breed_obj[breed_id]
        ring = ring_by_breed.get(breed_id)
        judge_id = ring.judge_id if ring and ring.judge_id else judge_id_by_breed.get(breed_id)
        judge_user = await _load_user_with_profile(db, judge_id)
        judge = judge_display(judge_user) if judge_user else None
        ring_date = (
            _fmt_date_long(ring.ring_date) if ring and ring.ring_date
            else _fmt_date_long(show.date_start)
        )
        sheets.append(
            _shape_ring_sheet(
                RingSheetInput(
                    organizer=show.venue or show.city,
                    show_title=show_title,
                    breed=breed.name,
                    judge=judge,
                    date=ring_date,
                    ring_number=ring.ring_number if ring else None,
                    catalog_numbers=numbers_by_breed[breed_id],
                )
            )
        )

    return {"sheets": sheets}


# ---------------------------------------------------------------------
# Каталог
# ---------------------------------------------------------------------


@dataclass
class CatalogMeta:
    show_name: str
    show_rank: str
    period: str
    city: str | None
    venue: str | None
    judges: list[dict]  # [{"name":..., "assignment":...}]


@dataclass
class CatalogEntryInput:
    group_number: int | None
    group_name: str | None
    breed_name: str
    fci_number: str | None
    breed_judge: str | None
    class_name: str
    sex: str  # "male"|"female"
    catalog_number: int | None
    dog_name: str
    date_of_birth: str
    color: str | None
    pedigree: str | None
    tattoo: str | None
    microchip: str | None
    breeder: str | None
    owner: str | None
    sire: str | None
    dam: str | None


def _shape_catalog_entry(e: CatalogEntryInput) -> dict:
    marks = " / ".join(x for x in [_s(e.tattoo), _s(e.microchip)] if x)
    dob = _s(e.date_of_birth)
    color = _s(e.color)
    pedigree = _s(e.pedigree)
    breeder = _s(e.breeder)
    owner = _s(e.owner)
    parents = " x ".join(x for x in [_s(e.sire), _s(e.dam)] if x)
    # Готовая строка-описание для тела каталога: только непустые части,
    # чтобы не было «висячих» запятых при отсутствующих полях.
    detail_line = ", ".join(
        p for p in [
            pedigree,
            marks,
            f"д.р. {dob}" if dob else "",
            color,
            parents,
            f"зав. {breeder}" if breeder else "",
            f"вл. {owner}" if owner else "",
        ] if p
    )
    return {
        "catalog_number": _s(e.catalog_number),
        "dog_name": _s(e.dog_name),
        "dob": dob,
        "color": color,
        "pedigree": pedigree,
        "marks": marks,
        "breeder": breeder,
        "owner": owner,
        "sire": _s(e.sire),
        "dam": _s(e.dam),
        "detail_line": detail_line,
    }


def _shape_catalog(meta: CatalogMeta, entries: list[CatalogEntryInput]) -> dict:
    """Группирует плоский список записей в группы FCI → породы → классы(+пол)."""
    def gkey(n: int | None) -> int:
        return n if n is not None else 999

    groups: dict[int, dict] = {}
    for e in entries:
        g = groups.setdefault(
            gkey(e.group_number),
            {
                "group_number": _s(e.group_number),
                "group_name": _s(e.group_name),
                "_breeds": {},
            },
        )
        b = g["_breeds"].setdefault(
            e.breed_name,
            {
                "breed_name": _s(e.breed_name),
                "fci_number": _s(e.fci_number),
                "judge": _s(e.breed_judge),
                "_classes": {},
            },
        )
        ckey = (e.class_name, e.sex)
        c = b["_classes"].setdefault(
            ckey,
            {
                "class_name": _s(e.class_name),
                "sex": _SEX_RU.get(e.sex, _s(e.sex)),
                "entries": [],
            },
        )
        c["entries"].append(_shape_catalog_entry(e))

    out_groups = []
    for gnum in sorted(groups.keys()):
        g = groups[gnum]
        breeds = []
        for bname in sorted(g["_breeds"].keys()):
            b = g["_breeds"][bname]
            classes = [b["_classes"][k] for k in b["_classes"]]
            # Для сводной таблицы «Породы по группам»: диапазон номеров по
            # каталогу (РКФ нумерует породу подряд) и число участников.
            nums: list[int] = []
            entry_count = 0
            for cls in classes:
                for e in cls["entries"]:
                    entry_count += 1
                    cn = e["catalog_number"]
                    if cn:
                        nums.append(int(cn))
            if not nums:
                catalog_range = ""
            elif len(nums) == 1:
                catalog_range = str(nums[0])
            else:
                catalog_range = f"{min(nums)}-{max(nums)}"
            breeds.append(
                {
                    "breed_name": b["breed_name"],
                    "fci_number": b["fci_number"],
                    "judge": b["judge"],
                    "catalog_range": catalog_range,
                    "entry_count": entry_count,
                    "classes": classes,
                }
            )
        out_groups.append(
            {
                "group_number": g["group_number"],
                "group_name": g["group_name"],
                "breeds": breeds,
            }
        )

    return {
        "show_name": _s(meta.show_name),
        "show_rank": _s(meta.show_rank),
        "period": _s(meta.period),
        "city": _s(meta.city),
        "venue": _s(meta.venue),
        "judges": meta.judges,
        "groups": out_groups,
        "total_entries": len(entries),
    }


async def build_catalog_context(
    db: AsyncSession, show_id: uuid.UUID
) -> dict:
    show = await db.get(Show, show_id)
    if show is None:
        raise ValueError("not_found")
    rank = await db.get(ShowRank, show.rank_id)

    judges = (
        await db.execute(select(ShowJudge).where(ShowJudge.show_id == show_id))
    ).scalars().all()
    judges_meta: list[dict] = []
    judge_for_breed: dict[uuid.UUID, str] = {}
    for j in judges:
        assignment = "—"
        if j.breed_id is not None:
            br = await db.get(Breed, j.breed_id)
            if br is not None:
                assignment = f"порода: {br.name}"
            ju = await _load_user_with_profile(db, j.judge_id)
            if ju is not None:
                judge_for_breed[j.breed_id] = judge_display(ju)
        elif j.breed_group_id is not None:
            grp = await db.get(BreedGroup, j.breed_group_id)
            if grp is not None:
                assignment = f"группа FCI {grp.number}: {grp.name}"
        ju = await _load_user_with_profile(db, j.judge_id)
        judges_meta.append(
            {"name": judge_display(ju) if ju else "—", "assignment": assignment}
        )

    entries = (
        await db.execute(
            select(ShowEntry)
            .where(ShowEntry.show_id == show_id)
            .order_by(ShowEntry.catalog_number.asc().nullslast())
        )
    ).scalars().all()

    inputs: list[CatalogEntryInput] = []
    for e in entries:
        dog = await db.get(Dog, e.dog_id)
        if dog is None:
            continue
        breed = await db.get(Breed, dog.breed_id)
        group = (
            await db.get(BreedGroup, breed.breed_group_id)
            if breed and breed.breed_group_id
            else None
        )
        cls = await db.get(ShowClass, e.show_class_id)
        breeder, _prefix = await _resolve_breeder(db, dog)
        owner = await _resolve_owner(db, dog)
        sire = await db.get(Dog, dog.father_id) if dog.father_id else None
        dam = await db.get(Dog, dog.mother_id) if dog.mother_id else None
        inputs.append(
            CatalogEntryInput(
                group_number=group.number if group else None,
                group_name=group.name if group else None,
                breed_name=breed.name if breed else "",
                fci_number=breed.fci_number if breed else None,
                breed_judge=judge_for_breed.get(dog.breed_id),
                class_name=cls.name if cls else "",
                sex=dog.sex.value,
                catalog_number=e.catalog_number,
                dog_name=dog.name,
                date_of_birth=_fmt_date(dog.date_of_birth),
                color=dog.color,
                pedigree=dog.rkf_number,
                tattoo=dog.tattoo,
                microchip=dog.microchip,
                breeder=breeder,
                owner=owner,
                sire=sire.name if sire else None,
                dam=dam.name if dam else None,
            )
        )

    period = _fmt_date(show.date_start) + (
        f" — {_fmt_date(show.date_end)}" if show.date_end else ""
    )
    meta = CatalogMeta(
        show_name=show.name,
        show_rank=rank.name if rank else "",
        period=period,
        city=show.city,
        venue=show.venue,
        judges=judges_meta,
    )
    return _shape_catalog(meta, inputs)


# ---------------------------------------------------------------------
# Readiness (чек-лист пробелов перед печатью)
# ---------------------------------------------------------------------


@dataclass
class EntryCheck:
    catalog_number: int | None
    dog_name: str
    owner_present: bool
    breeder_present: bool
    has_tattoo: bool
    has_microchip: bool
    has_pedigree: bool


def _entry_issues(c: EntryCheck) -> list[dict]:
    issues: list[dict] = []
    if c.catalog_number is None:
        issues.append({"code": "no_catalog_number", "message": "нет номера каталога"})
    if not c.owner_present:
        issues.append({"code": "no_owner", "message": "не указан владелец (ФИО)"})
    if not c.breeder_present:
        issues.append({"code": "no_breeder", "message": "не указан заводчик"})
    if not (c.has_tattoo or c.has_microchip):
        issues.append({"code": "no_id", "message": "нет клейма и чипа"})
    if not c.has_pedigree:
        issues.append({"code": "no_pedigree", "message": "нет № родословной"})
    return issues


async def build_documents_readiness(
    db: AsyncSession, show_id: uuid.UUID
) -> dict:
    """Список записей с проблемами, мешающими корректной печати документов."""
    show = await db.get(Show, show_id)
    if show is None:
        raise ValueError("not_found")
    entries = (
        await db.execute(select(ShowEntry).where(ShowEntry.show_id == show_id))
    ).scalars().all()

    problems: list[dict] = []
    for e in entries:
        dog = await db.get(Dog, e.dog_id)
        if dog is None:
            continue
        owner = await _resolve_owner(db, dog)
        breeder, _p = await _resolve_breeder(db, dog)
        check = EntryCheck(
            catalog_number=e.catalog_number,
            dog_name=dog.name,
            owner_present=bool(owner),
            breeder_present=bool(breeder),
            has_tattoo=bool(dog.tattoo),
            has_microchip=bool(dog.microchip),
            has_pedigree=bool(dog.rkf_number),
        )
        issues = _entry_issues(check)
        if issues:
            problems.append(
                {
                    "entry_id": str(e.id),
                    "dog_name": dog.name,
                    "catalog_number": e.catalog_number,
                    "issues": issues,
                }
            )
    return {"total_entries": len(entries), "problems": problems}
