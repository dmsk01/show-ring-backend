r"""
Демо-сид для ручного тестирования UI: наполняет БД разнообразными
данными по ВСЕМ разделам с фронтенд-ручками — питомники (с аватарами),
собаки с родословной, владельцами и фото (+ превью file_variants), помёты
во всех статусах, выставки во всех статусах (allow-list пород, ринги,
судьи на породу и на группу, записи, результаты с титулами по реальным
правилам РКФ из app.services.show_rules, dog_titles), объявления с фото
во всех статусах/доступностях, реклама (кампании во всех статусах,
баннеры во всех местах, события показов/кликов), блог с обложками,
подписки и уведомления, тикеты поддержки с перепиской, лог модерации,
журнал безопасности аккаунта, фоновые задачи (с реальным PDF в MinIO)
и тиры квот. Создаются роли admin/operator/organizer/judge/breeder/buyer —
чтобы пройти и админские, и пользовательские ручки.

Покрытие «моих» разделов:
- /users/me/dogs — у всех собак проставлен owner_id (заводчики), один
  щенок «продан» покупателю buyer1.
- /dashboard/my-shows — записи на выставки оформлены от имени владельца
  собаки (registered_by), поэтому у заводчиков и buyer1 есть и
  активные, и прошедшие выставки.

Не сеются чисто инфраструктурные таблицы без фронтенд-ручек:
outbox_events (наполняется воркером), refresh/email-токены (создаются
при логине/верификации).

В отличие от scripts.seed_test_show (узкий сценарий «одна завершённая
выставка для генерации документов»), этот скрипт даёт «широту»: списки,
фильтры, пагинацию есть на чём проверить.

Запуск:
    .\venv\Scripts\python.exe -m scripts.seed_demo

Нужны PostgreSQL (с накатанными миграциями) и MinIO (фото, обложки, PDF).

Даты:
- Опорная дата — сегодняшний день (TODAY = date.today()). Даты выставок,
  кампаний, помётов пересчитываются при каждом запуске, чтобы
  «предстоящие» выставки не уезжали в прошлое.

Идемпотентность:
- Справочники гарантируются через scripts.seed_references.
- Сущности ищутся по натуральным ключам (email, kennel_prefix,
  rkf_number, имя выставки/объявления/кампании, slug поста).
- Демо-выставки пересобираются к эталонному состоянию при каждом запуске:
  записи, результаты и dog_titles этих выставок приводятся к спеке сида
  (лишние удаляются). Правки, сделанные через UI в демо-выставках,
  повторный запуск откатывает — это осознанно.
- Файлы (фото, обложки, аватары) догружаются только если их нет.

Чтобы не конфликтовать с seed_test_show (общие UNIQUE на rkf_number,
kennel_prefix, email), здесь используются отдельные пространства имён:
домены *-demo@dogshow.ru, приставки «… (демо)», номера RKF-DEMO-*.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import ClientError, EndpointConnectionError
from PIL import Image, ImageDraw
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, engine

# Регистрируем все модели в Base.metadata (ленивые FK).
import app.models.ad  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.classified  # noqa: F401
import app.models.dog  # noqa: F401
import app.models.file  # noqa: F401
import app.models.kennel  # noqa: F401
import app.models.litter  # noqa: F401
import app.models.notification  # noqa: F401
import app.models.outbox  # noqa: F401
import app.models.post  # noqa: F401
import app.models.reference  # noqa: F401
import app.models.result  # noqa: F401
import app.models.security_audit  # noqa: F401
import app.models.show  # noqa: F401
import app.models.support  # noqa: F401
import app.models.task  # noqa: F401
import app.models.upload_quota  # noqa: F401
from app.models.ad import (
    AdBanner,
    AdCampaign,
    AdEvent,
    AdEventType,
    BannerPlacement,
    CampaignStatus,
)
from app.models.audit import ModerationLog
from app.models.classified import (
    AnimalAvailability,
    Classified,
    ClassifiedCategory,
    ClassifiedImage,
    ClassifiedPriceKind,
    ClassifiedStatus,
)
from app.models.dog import Dog, DogPhoto, SexEnum
from app.models.file import FileVariant, UploadedFile
from app.models.kennel import Kennel
from app.models.litter import Litter, LitterStatus
from app.models.notification import (
    EventType,
    Notification,
    NotificationChannel,
    NotificationStatus,
    Subscription,
)
from app.models.post import Post, PostPublish
from app.models.reference import Breed, Grade, ShowClass, ShowRank, Title
from app.models.result import DogTitle, ShowResult
from app.models.security_audit import SecurityAuditLog
from app.models.show import (
    Show,
    ShowBreed,
    ShowEntry,
    ShowJudge,
    ShowRing,
    ShowStatus,
)
from app.models.support import (
    SupportMessage,
    SupportTicket,
    TicketPriority,
    TicketStatus,
)
from app.models.task import Task, TaskStatusEnum
from app.models.upload_quota import UploadQuotaTier
from app.models.user import RoleEnum, User, UserProfile, UserRole
from app.services import file_storage, show_rules
from app.utils.image_processing import VARIANTS, make_variant
from app.utils.security import hash_password
from scripts.seed_references import seed as seed_references

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed-demo")

DEMO_PASSWORD = "TestPass123!"
SHOW_COMPLETED = "Кубок столицы — 2026 (демо)"
TODAY = date.today()
# Префикс публичного URL файлов для полей-строк (Post.cover_url). Фронт
# по умолчанию ходит на API через прокси /api (CONFIG.serverUrl).
FILES_BASE_URL = os.environ.get("SEED_FILES_BASE_URL", "/api").rstrip("/")


# ---------------------------------------------------------------------
# Хелперы (те же, что в seed_references / seed_test_show)
# ---------------------------------------------------------------------


async def _get_or_create(db, model, lookup: dict, create: dict):
    stmt = select(model)
    for k, v in lookup.items():
        stmt = stmt.where(getattr(model, k) == v)
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if obj is not None:
        return obj, False
    obj = model(**create)
    db.add(obj)
    await db.flush()
    return obj, True


async def _upsert(db, model, lookup: dict, create: dict, update_: dict):
    """
    _get_or_create + принудительное обновление полей `update_` у найденной
    строки. Для полей, которые сид «держит» в эталонном состоянии (даты
    относительно TODAY, статусы демо-выставок, добавленные позже колонки).
    """
    obj, created = await _get_or_create(
        db, model, lookup, {**create, **update_}
    )
    if not created:
        for k, v in update_.items():
            setattr(obj, k, v)
    return obj, created


async def _count(db, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    for cond in where:
        stmt = stmt.where(cond)
    return int((await db.execute(stmt)).scalar_one())


async def _user_with_profile(
    db, email, *, last, first, patr=None, country="Россия", role=None,
    phone=None, is_phone_verified=False, socials=None,
) -> User:
    # UserProfile хранит ФИО + страну + соцсети (город живёт у питомника /
    # объявления), поэтому city здесь не принимаем.
    user, _ = await _get_or_create(
        db, User, {"email": email},
        {
            "email": email,
            # phone опционален — заполняем только у части юзеров, чтобы
            # в UserResponse было видно и заполненный, и пустой телефон.
            "phone": phone,
            "hashed_password": hash_password(DEMO_PASSWORD),
            "is_active": True,
            "is_email_verified": True,
            "is_phone_verified": is_phone_verified,
        },
    )
    # phone/верификацию проставляем и существующим юзерам (БД могла быть
    # засеяна прошлой версией сида без этих полей). _get_or_create не
    # обновляет найденные строки, поэтому делаем это явно и идемпотентно.
    if phone is not None:
        user.phone = phone
        user.is_phone_verified = is_phone_verified

    # socials — dict из {instagram, facebook, vk, telegram}; None = без сетей.
    socials = socials or {}
    profile, _ = await _get_or_create(
        db, UserProfile, {"user_id": user.id},
        {
            "user_id": user.id, "last_name": last, "first_name": first,
            "patronymic": patr, "country": country,
            "instagram": socials.get("instagram"),
            "facebook": socials.get("facebook"),
            "vk": socials.get("vk"),
            "telegram": socials.get("telegram"),
        },
    )
    # Те же соображения, что и для phone: обновляем соцсети существующему
    # профилю (по той же причине идемпотентности на уже засеянной БД).
    if socials:
        profile.instagram = socials.get("instagram")
        profile.facebook = socials.get("facebook")
        profile.vk = socials.get("vk")
        profile.telegram = socials.get("telegram")
    if role is not None:
        await _get_or_create(
            db, UserRole, {"user_id": user.id, "role": role},
            {"user_id": user.id, "role": role, "granted_by": user.id},
        )
    return user


# ---------------------------------------------------------------------
# Файлы: генерация заглушек, загрузка в MinIO, варианты (превью)
# ---------------------------------------------------------------------

# Палитра фоновых цветов заглушечных картинок (детерминированно по id).
_PHOTO_PALETTE: list[tuple[int, int, int]] = [
    (76, 110, 159), (159, 76, 76), (76, 159, 99),
    (150, 120, 60), (110, 76, 159), (60, 130, 140),
]


def _color_for(key: uuid.UUID) -> tuple[int, int, int]:
    return _PHOTO_PALETTE[key.int % len(_PHOTO_PALETTE)]


def _make_image_bytes(
    label: str, color: tuple[int, int, int], size=(640, 480)
) -> bytes:
    """Заглушечный JPEG: цветной фон + подпись снизу."""
    w, h = size
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, h - 88, w, h), fill=(0, 0, 0))
    draw.text((24, h - 60), label[:48], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _make_pdf_bytes(lines: list[str]) -> bytes:
    """Одностраничный PDF-заглушка (A4 @72dpi) — Pillow умеет save(PDF)."""
    img = Image.new("RGB", (595, 842), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((48, 60 + i * 22), line, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


async def _create_variants(
    db: AsyncSession, file_id: uuid.UUID, original: bytes
) -> int:
    """
    То же, что делает воркер process_image (worker/handlers/file_handler.py):
    thumb + medium с водяным знаком в MinIO и строки file_variants. Сид
    не ставит задачу в очередь — генерирует варианты сам, чтобы превью
    были и без запущенного воркера.
    """
    n = 0
    for kind, max_size, watermark in VARIANTS:
        jpeg, width, height = await asyncio.to_thread(
            make_variant, original, max_size, watermark
        )
        s3_key, size = await file_storage.upload_bytes(
            jpeg, content_type="image/jpeg", extension="jpg", folder="variants"
        )
        db.add(FileVariant(
            file_id=file_id, kind=kind, s3_key=s3_key,
            content_type="image/jpeg", width=width, height=height,
            has_watermark=watermark, size_bytes=size,
        ))
        n += 1
    await db.flush()
    return n


async def _upload_image(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    label: str,
    color: tuple[int, int, int],
    folder: str,
    filename: str,
    size=(640, 480),
) -> UploadedFile:
    """Генерирует JPEG, грузит в MinIO, создаёт UploadedFile + варианты."""
    data = _make_image_bytes(label, color, size)
    # upload_bytes не валидирует magic bytes — содержимое мы сами
    # сформировали Pillow'ом, оно гарантированно валидный JPEG.
    s3_key, nbytes = await file_storage.upload_bytes(
        data, content_type="image/jpeg", extension="jpg", folder=folder,
    )
    uploaded = UploadedFile(
        uploaded_by=owner_id,
        s3_key=s3_key,
        original_filename=filename,
        content_type="image/jpeg",
        size_bytes=nbytes,
        # Фото/аватары/обложки публичны, отдаются GET /files/{id}.
        is_public=True,
    )
    db.add(uploaded)
    await db.flush()
    await _create_variants(db, uploaded.id, data)
    return uploaded


async def _attach_photos(
    db: AsyncSession, dog: Dog, owner_id: uuid.UUID, count: int
) -> int:
    """
    Грузит до `count` заглушечных фото собаки и связывает их через
    DogPhoto. Идемпотентно: догружает только недостающие позиции.
    Возвращает число реально добавленных фото.
    """
    existing = await _count(db, DogPhoto, DogPhoto.dog_id == dog.id)
    added = 0
    for pos in range(existing, count):
        label = dog.name if pos == 0 else f"{dog.name} · {pos + 1}"
        uploaded = await _upload_image(
            db, owner_id=owner_id, label=label, color=_color_for(dog.id),
            folder="dogs", filename=f"{dog.name}-{pos + 1}.jpg",
        )
        db.add(DogPhoto(
            dog_id=dog.id, file_id=uploaded.id, position=pos,
            is_primary=(pos == 0),
        ))
        added += 1
    await db.flush()
    return added


async def _attach_classified_images(
    db: AsyncSession, cl: Classified, count: int
) -> int:
    existing = await _count(
        db, ClassifiedImage, ClassifiedImage.classified_id == cl.id
    )
    added = 0
    for pos in range(existing, count):
        uploaded = await _upload_image(
            db, owner_id=cl.author_id, label=f"{cl.title} · {pos + 1}",
            color=_color_for(cl.id), folder="classifieds",
            filename=f"classified-{pos + 1}.jpg",
        )
        db.add(ClassifiedImage(
            classified_id=cl.id, file_id=uploaded.id, position=pos,
            is_primary=(pos == 0),
        ))
        added += 1
    await db.flush()
    return added


async def _ensure_avatar(db: AsyncSession, obj, owner_id, label: str) -> None:
    """Аватар для User/Kennel — только если его ещё нет."""
    if obj.avatar_file_id is not None:
        return
    uploaded = await _upload_image(
        db, owner_id=owner_id, label=label, color=_color_for(obj.id),
        folder="avatars", filename="avatar.jpg", size=(400, 400),
    )
    obj.avatar_file_id = uploaded.id


async def _backfill_variants(db: AsyncSession) -> int:
    """
    Изображения, загруженные прошлой версией сида (или через UI без
    запущенного воркера), не имеют превью. Досоздаём их — как воркер.
    """
    rows = (await db.execute(
        select(UploadedFile)
        .where(UploadedFile.content_type.like("image/%"))
        .where(~UploadedFile.variants.any())
    )).scalars().all()
    n = 0
    for f in rows:
        try:
            original, _ctype = await file_storage.get_file_stream(f.s3_key)
        except ClientError:
            logger.warning("backfill variants: нет оригинала %s", f.s3_key)
            continue
        try:
            n += await _create_variants(db, f.id, original)
        except OSError:
            # Pillow не смог открыть (битый/не-изображение) — пропускаем.
            logger.warning("backfill variants: не изображение %s", f.s3_key)
    return n


# ---------------------------------------------------------------------
# Выставки: записи + результаты по правилам РКФ
# ---------------------------------------------------------------------


@dataclass
class EntrySpec:
    dog: Dog
    owner_id: uuid.UUID          # → ShowEntry.registered_by («мои выставки»)
    class_code: str | None = None  # None — класс по возрасту
    handler_id: uuid.UUID | None = None
    notes: str | None = None
    absent: bool = False         # оценка «Отсутствует»


@dataclass
class RingSpec:
    breed: Breed
    judge: User
    time_start: time


@dataclass
class Refs:
    classes: dict[str, ShowClass]
    grades: dict[str, Grade]
    titles: dict[str, Title]


def _class_by_age(age_months: int) -> str | None:
    """Самый «узкий» подходящий класс РКФ по возрасту на дату выставки."""
    if age_months < 4:
        return None
    if age_months < 6:
        return "baby"
    if age_months < 9:
        return "puppy"
    if age_months < 15:
        return "junior"
    if age_months <= 24:
        return "intermediate"
    return "open"


_CLASS_ORDER = [
    "baby", "puppy", "junior", "intermediate", "open", "working",
    "champions", "veteran",
]
_ADULT_GRADES = ["excellent", "excellent", "very-good", "good"]
_PUPPY_GRADES = ["great-promise", "promising", "less-promising"]
_CRITIQUE = {
    "excellent": "Отличный породный тип, гармоничное сложение, крепкий "
                 "костяк. Свободные размашистые движения, отличная подача.",
    "very-good": "Хороший тип и пропорции, правильный прикус. Движения "
                 "могли бы быть свободнее, немного мягкая спина.",
    "good": "Типичная голова, но недостаточно выражены углы конечностей, "
            "движения скованные. Нужна работа над кондицией.",
    "great-promise": "Очень перспективный щенок: отличный тип, уверенный "
                     "темперамент, правильные движения для возраста.",
    "promising": "Перспективный щенок, пропорциональный, нужно время.",
    "less-promising": "Пока недостаточно сформирован, подождать.",
}


def _pick_best(
    cands: list[tuple[ShowEntry, str, set[str]]], sex: SexEnum,
) -> ShowEntry | None:
    """Лучший кобель/сука: победитель класса с CAC, иначе любой CW."""
    pool = [(e, c, codes) for e, c, codes in cands if e.dog_sex == sex]
    for e, _c, codes in pool:
        if show_rules.TITLE_CAC in codes:
            return e
    return pool[0][0] if pool else None


async def _build_show_graph(
    db: AsyncSession,
    show: Show,
    *,
    rank: ShowRank,
    rings: list[RingSpec],
    entries: list[EntrySpec],
    refs: Refs,
    assign_catalog: bool,
    result_breed_ids: set[uuid.UUID],
    big_bis: bool = False,
    group_judge: User | None = None,
) -> None:
    """
    Приводит граф выставки (ринги, судьи, записи, результаты, dog_titles)
    к спеке. Идемпотентно: повторный вызов даёт то же состояние.
    """
    # 1. Ринги (натуральный ключ — выставка + порода) и назначения судей.
    for i, r in enumerate(rings, start=1):
        await _upsert(
            db, ShowRing, {"show_id": show.id, "breed_id": r.breed.id},
            {"show_id": show.id, "breed_id": r.breed.id},
            {
                "ring_number": i, "judge_id": r.judge.id,
                "ring_date": show.date_start, "time_start": r.time_start,
                "time_end": time(r.time_start.hour + 2, 0),
                "location": f"Ринг №{i}",
            },
        )
        await _get_or_create(
            db, ShowJudge,
            {"show_id": show.id, "judge_id": r.judge.id, "breed_id": r.breed.id},
            {"show_id": show.id, "judge_id": r.judge.id, "breed_id": r.breed.id},
        )
    # Судья на группу FCI (ShowJudge.breed_group_id) + ринг BIG.
    if group_judge is not None and rings and rings[0].breed.breed_group_id:
        group_id = rings[0].breed.breed_group_id
        await _get_or_create(
            db, ShowJudge,
            {"show_id": show.id, "judge_id": group_judge.id,
             "breed_group_id": group_id},
            {"show_id": show.id, "judge_id": group_judge.id,
             "breed_group_id": group_id},
        )
        await _upsert(
            db, ShowRing,
            {"show_id": show.id, "breed_group_id": group_id},
            {"show_id": show.id, "breed_group_id": group_id},
            {
                "ring_number": len(rings) + 1, "judge_id": group_judge.id,
                "ring_date": show.date_end or show.date_start,
                "time_start": time(16, 0), "time_end": time(17, 0),
                "location": "Главный ринг (BIG/BIS)",
            },
        )

    # 2. Сносим то, что пересчитывается: титулы выставки и лишние записи.
    await db.execute(delete(DogTitle).where(DogTitle.show_id == show.id))
    wanted_dogs = [e.dog.id for e in entries]
    await db.execute(
        delete(ShowEntry).where(
            ShowEntry.show_id == show.id, ShowEntry.dog_id.not_in(wanted_dogs)
        )
    )
    # Номера каталога переназначаем с нуля — иначе UNIQUE(show, catalog)
    # может сработать на промежуточном состоянии.
    await db.execute(
        update(ShowEntry).where(ShowEntry.show_id == show.id)
        .values(catalog_number=None)
    )
    await db.flush()

    # 3. Записи. Каталог — по порядку ринга, затем класса, затем пола.
    ring_order = {r.breed.id: i for i, r in enumerate(rings)}
    resolved: list[tuple[EntrySpec, str]] = []
    for spec in entries:
        code = spec.class_code
        if code is None:
            age = show_rules.age_in_months_on(
                spec.dog.date_of_birth, show.date_start
            )
            code = _class_by_age(age)
        if code is None:
            continue
        resolved.append((spec, code))
    resolved.sort(key=lambda t: (
        ring_order.get(t[0].dog.breed_id, 99),
        _CLASS_ORDER.index(t[1]),
        t[0].dog.sex.value != "male",
        t[0].dog.name,
    ))

    entry_rows: list[tuple[ShowEntry, EntrySpec, str]] = []
    for n, (spec, code) in enumerate(resolved, start=1):
        entry, _ = await _upsert(
            db, ShowEntry, {"show_id": show.id, "dog_id": spec.dog.id},
            {"show_id": show.id, "dog_id": spec.dog.id},
            {
                "show_class_id": refs.classes[code].id,
                "registered_by": spec.owner_id,
                "handler_id": spec.handler_id,
                "notes": spec.notes,
                "catalog_number": n if assign_catalog else None,
            },
        )
        entry.dog_sex = spec.dog.sex  # транзиентное поле для _pick_best
        entry_rows.append((entry, spec, code))
    await db.flush()

    # 4. Результаты. Записи без результата по спеке — очищаем.
    no_result_ids = [
        e.id for e, spec, _ in entry_rows
        if spec.dog.breed_id not in result_breed_ids
    ]
    if no_result_ids:
        await db.execute(
            delete(ShowResult).where(ShowResult.show_entry_id.in_(no_result_ids))
        )

    ring_judge = {r.breed.id: r.judge for r in rings}
    for ri, ring in enumerate(rings):
        if ring.breed.id not in result_breed_ids:
            continue
        animal_type_id = ring.breed.animal_type_id
        ring_entries = [
            t for t in entry_rows if t[1].dog.breed_id == ring.breed.id
        ]
        # entry.id → (grade, placement, flags, awards)
        state: dict[uuid.UUID, dict] = {}
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for t in ring_entries:
            groups[(t[2], t[1].dog.sex.value)].append(t)

        class_winners: list[tuple[ShowEntry, str, set[str]]] = []
        for (code, _sex), items in groups.items():
            place = 0
            for entry, spec, _code in items:
                if spec.absent:
                    grade = refs.grades["absent"]
                    placement = None
                else:
                    pool = (_PUPPY_GRADES if code in ("baby", "puppy")
                            else _ADULT_GRADES)
                    grade = refs.grades[pool[min(place, len(pool) - 1)]]
                    place += 1
                    placement = place if place <= 4 else None
                awards = await show_rules.compute_class_titles(
                    db, animal_type_id=animal_type_id,
                    show_class=refs.classes[code], show_rank=rank,
                    grade=grade, placement=placement,
                )
                codes = {a.code for a in awards}
                st = {
                    "grade": grade, "placement": placement,
                    "awards": list(awards), "flags": {},
                    "entry": entry, "dog": spec.dog,
                }
                if show_rules.TITLE_CW in codes:
                    st["flags"]["is_class_winner"] = True
                    class_winners.append((entry, code, codes))
                state[entry.id] = st

        # Лучший кобель / сука → BOB / BOS (BOB чередуем по рингам).
        best_m = _pick_best(class_winners, SexEnum.male)
        best_f = _pick_best(class_winners, SexEnum.female)
        bob, bos = (best_m, best_f) if ri % 2 == 0 else (best_f, best_m)
        if bob is None:
            bob, bos = bos, None
        for e, flag in ((best_m, "is_best_male"), (best_f, "is_best_female")):
            if e is None:
                continue
            state[e.id]["flags"][flag] = True
            state[e.id]["awards"] += await show_rules.get_best_of_breed_titles(
                db, animal_type_id=animal_type_id, show_rank=rank,
                is_bob=(e is bob), is_best_male=(e is best_m),
                is_best_female=(e is best_f),
            )
        if bob is not None:
            state[bob.id]["flags"]["is_best_of_breed"] = True
        if bos is not None and "bos" in refs.titles:
            state[bos.id]["awards"].append(_award(refs.titles["bos"]))
        for e, code, _codes in class_winners:
            if code == "junior":
                state[e.id]["flags"]["is_best_junior"] = True
                break
        for e, code, _codes in class_winners:
            if code == "veteran":
                state[e.id]["flags"]["is_best_veteran"] = True
                if "vw" in refs.titles:
                    state[e.id]["awards"].append(_award(refs.titles["vw"]))
                break

        # BIG/BIS — BOB первого ринга; R.BIG — BOB второго.
        if big_bis and bob is not None:
            if ri == 0:
                state[bob.id]["flags"]["is_best_in_group"] = True
                state[bob.id]["flags"]["is_best_in_show"] = True
                for award in (
                    await show_rules.get_big_title(db, animal_type_id),
                    await show_rules.get_bis_title(db, animal_type_id),
                ):
                    if award is not None:
                        state[bob.id]["awards"].append(award)
            elif ri == 1 and "r-big" in refs.titles:
                state[bob.id]["awards"].append(_award(refs.titles["r-big"]))

        # Запись результатов + источник истины dog_titles.
        for st in state.values():
            awards, seen = [], set()
            for a in st["awards"]:
                if a.code not in seen:
                    seen.add(a.code)
                    awards.append(a)
            flags = st["flags"]
            grade = st["grade"]
            await _upsert(
                db, ShowResult, {"show_entry_id": st["entry"].id},
                {"show_entry_id": st["entry"].id},
                {
                    "judge_id": ring_judge[ring.breed.id].id,
                    "grade_id": grade.id,
                    "placement": st["placement"],
                    "is_class_winner": flags.get("is_class_winner", False),
                    "is_best_male": flags.get("is_best_male", False),
                    "is_best_female": flags.get("is_best_female", False),
                    "is_best_of_breed": flags.get("is_best_of_breed", False),
                    "is_best_junior": flags.get("is_best_junior", False),
                    "is_best_veteran": flags.get("is_best_veteran", False),
                    "is_best_in_group": flags.get("is_best_in_group", False),
                    "is_best_in_show": flags.get("is_best_in_show", False),
                    "critique": _CRITIQUE.get(grade.code),
                    "titles_cache": [
                        {"code": a.code, "name": a.name} for a in awards
                    ] or None,
                },
            )
            for a in awards:
                db.add(DogTitle(
                    dog_id=st["dog"].id, title_id=a.title_id, show_id=show.id,
                    judge_id=ring_judge[ring.breed.id].id,
                    date_earned=show.date_start,
                ))
    await db.flush()


def _award(t: Title) -> show_rules.TitleAward:
    return show_rules.TitleAward(title_id=t.id, code=t.code, name=t.name)


async def _set_show_breeds(
    db: AsyncSession, show: Show, breeds: list[Breed]
) -> None:
    """Allow-list пород выставки ровно = breeds (пустой — всепородная)."""
    ids = [b.id for b in breeds]
    await db.execute(
        delete(ShowBreed).where(
            ShowBreed.show_id == show.id, ShowBreed.breed_id.not_in(ids)
        )
    )
    for b in breeds:
        await _get_or_create(
            db, ShowBreed, {"show_id": show.id, "breed_id": b.id},
            {"show_id": show.id, "breed_id": b.id},
        )


# ---------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------


async def seed(db: AsyncSession) -> None:
    # 0. Справочники.
    await seed_references(db)

    refs = Refs(
        classes={c.code: c for c in (await db.execute(select(ShowClass))).scalars()},
        grades={g.code: g for g in (await db.execute(select(Grade))).scalars()},
        titles={t.code: t for t in (await db.execute(select(Title))).scalars()},
    )
    ranks = {r.code: r for r in (await db.execute(select(ShowRank))).scalars()}

    # 1. Люди: админ, оператор поддержки, организатор, два судьи, четыре
    #    заводчика, два покупателя. admin/operator нужны, чтобы протестировать
    #    /admin/* и /support/admin/* ручки на фронте.
    admin = await _user_with_profile(
        db, "admin-demo@dogshow.ru", last="Администраторов", first="Админ",
        role=RoleEnum.admin,
        phone="+79990000001", is_phone_verified=True,
    )
    operator = await _user_with_profile(
        db, "operator-demo@dogshow.ru", last="Операторова", first="Оксана",
        patr="Ивановна", role=RoleEnum.operator,
    )
    organizer = await _user_with_profile(
        db, "org-demo@dogshow.ru", last="Воронцова", first="Ирина",
        patr="Сергеевна", role=RoleEnum.organizer,
        socials={
            "vk": "https://vk.com/showring_org",
            "telegram": "https://t.me/showring_org",
        },
    )
    judge1 = await _user_with_profile(
        db, "judge1-demo@dogshow.ru", last="Лебедев", first="Андрей",
        patr="Викторович", role=RoleEnum.judge,
    )
    judge2 = await _user_with_profile(
        db, "judge2-demo@dogshow.ru", last="Климова", first="Ольга",
        patr="Павловна", role=RoleEnum.judge,
    )

    breeders_spec = [
        ("breeder1-demo@dogshow.ru", "Никитина", "Елена", "Аркадия",
         "Аркадия", "Москва", "+7 (495) 100-10-10", "arkadia-kennel.ru"),
        ("breeder2-demo@dogshow.ru", "Орлов", "Сергей", "Северная Звезда",
         "Северная Звезда", "Санкт-Петербург", "+7 (812) 200-20-20", None),
        ("breeder3-demo@dogshow.ru", "Зайцева", "Марина", "Золотая Долина",
         "Золотая Долина", "Казань", "+7 (843) 300-30-30", "zolotaya-dolina.ru"),
        ("breeder4-demo@dogshow.ru", "Громов", "Павел", "Верный Друг",
         "Верный Друг", "Екатеринбург", "+7 (343) 400-40-40", None),
    ]
    breeders: list[tuple[User, Kennel]] = []
    for i, (email, last, first, kname, prefix, city, phone, site) in enumerate(
        breeders_spec
    ):
        # Первому заводчику прописываем соцсети — чтобы блок соцссылок в
        # профиле/карточке питомника было на чём проверить.
        socials = (
            {
                "instagram": "https://instagram.com/arkadia_kennel",
                "vk": "https://vk.com/arkadia_kennel",
                "telegram": "https://t.me/arkadia_kennel",
            }
            if i == 0 else None
        )
        u = await _user_with_profile(
            db, email, last=last, first=first, role=RoleEnum.breeder,
            socials=socials,
        )
        kennel, _ = await _get_or_create(
            db, Kennel, {"kennel_prefix": f"{prefix} (демо)"},
            {
                "owner_id": u.id,
                "name": f"Питомник «{kname}»",
                "kennel_prefix": f"{prefix} (демо)",
                "city": city,
                "country": "Россия",
                "contact_phone": phone,
                "contact_email": email,
                "description": f"Племенное разведение, питомник «{kname}». "
                               "Щенки шоу- и брид-класса, документы РКФ/FCI.",
                # Часть питомников «проверена» — для зелёной галочки в UI.
                "is_verified": i % 2 == 0,
            },
        )
        # website — у половины питомников, чтобы видеть и пустое поле.
        kennel.website = f"https://{site}" if site else None
        breeders.append((u, kennel))

    buyer1 = await _user_with_profile(
        db, "buyer1-demo@dogshow.ru", last="Соколов", first="Дмитрий",
        role=RoleEnum.buyer,
        phone="+79990000002", is_phone_verified=True,
    )
    buyer2 = await _user_with_profile(
        db, "buyer2-demo@dogshow.ru", last="Морозова", first="Алина",
        role=RoleEnum.buyer,
    )
    handler_user = breeders[2][0]  # у неё объявление «Услуги хендлера»

    # 2. Породы — берём первые 6 из справочника (стабильно по имени).
    breeds = (
        await db.execute(select(Breed).order_by(Breed.name).limit(6))
    ).scalars().all()
    if len(breeds) < 6:
        raise SystemExit("Мало пород в справочнике — запусти seed_references")

    # 3. Собаки. Для каждого питомника — пара производителей (отец/мать)
    #    своей породы и несколько потомков от этой пары. Так в карточках
    #    собак появляется родословная отец×мать. owner_id — заводчик
    #    (эндпоинт /users/me/dogs работает по нему).
    colors = ["чёрный", "рыжий", "тигровый", "палевый", "бело-рыжий",
              "чёрно-подпалый", "голубой", "шоколадный"]
    sire_first = ["ГРАНД", "БАРОН", "ЦЕЗАРЬ", "ВИКОНТ", "АТАМАН", "МАГНАТ"]
    dam_first = ["ЛЕДИ", "АЛЬФА", "НИКА", "ГРАЦИЯ", "ВЕГА", "ЗАРА"]
    pup_first = ["РЕКС", "БЕЛЛА", "ТОР", "ЛЮНА", "ДЖЕК", "АЙРИС",
                 "МАКС", "ДИНА", "ЗЕВС", "МИРА"]

    rkf_counter = 0

    def next_rkf() -> str:
        nonlocal rkf_counter
        rkf_counter += 1
        return f"RKF-DEMO-{rkf_counter:04d}"

    # dogs_by_breed[breed_idx] = list[Dog] потомков (для записей на выставки)
    dogs_by_breed: dict[int, list[Dog]] = {}
    # parents_by_breed[breed_idx] = (sire, dam, breeder_user, kennel)
    parents_by_breed: dict[int, tuple[Dog, Dog, User, Kennel]] = {}
    # pups_by_pair[(bi, breed_idx)] = щенки пары — состав помёта
    pups_by_pair: dict[tuple[int, int], list[Dog]] = {}
    # Собаки, которым прицепим демо-фото: (dog, owner_id, сколько фото).
    dog_specs: list[tuple[Dog, uuid.UUID, int]] = []
    # Владелец каждой собаки (для записей на выставку).
    owner_of: dict[uuid.UUID, uuid.UUID] = {}

    for bi, (breeder_u, kennel) in enumerate(breeders):
        # Каждый заводчик «специализируется» на двух породах.
        breed_idxs = [bi % len(breeds), (bi + 2) % len(breeds)]
        for breed_idx in breed_idxs:
            breed = breeds[breed_idx]
            sire_rkf = f"RKF-DEMO-SIRE-{bi}-{breed_idx}"
            dam_rkf = f"RKF-DEMO-DAM-{bi}-{breed_idx}"
            sire, _ = await _get_or_create(
                db, Dog, {"rkf_number": sire_rkf},
                {
                    "breed_id": breed.id,
                    "name": f"{sire_first[bi % len(sire_first)]} "
                            f"{kennel.kennel_prefix}",
                    "sex": SexEnum.male,
                    "date_of_birth": date(2018, 3, 1),
                    "color": colors[breed_idx % len(colors)],
                    "rkf_number": sire_rkf,
                    "breeder_kennel_id": kennel.id,
                    "kennel_id": kennel.id,
                },
            )
            dam, _ = await _get_or_create(
                db, Dog, {"rkf_number": dam_rkf},
                {
                    "breed_id": breed.id,
                    "name": f"{dam_first[bi % len(dam_first)]} "
                            f"{kennel.kennel_prefix}",
                    "sex": SexEnum.female,
                    "date_of_birth": date(2019, 4, 1),
                    "color": colors[(breed_idx + 1) % len(colors)],
                    "rkf_number": dam_rkf,
                    "breeder_kennel_id": kennel.id,
                    "kennel_id": kennel.id,
                },
            )
            for d in (sire, dam):
                d.owner_id = breeder_u.id
                owner_of[d.id] = breeder_u.id
            parents_by_breed[breed_idx] = (sire, dam, breeder_u, kennel)
            dog_specs.append((sire, breeder_u.id, 1))
            dog_specs.append((dam, breeder_u.id, 1))

            # 3 потомка от этой пары.
            for k in range(3):
                sex = SexEnum.male if k % 2 == 0 else SexEnum.female
                name = (f"{pup_first[(bi + k) % len(pup_first)]} "
                        f"{kennel.kennel_prefix}")
                rkf = next_rkf()
                # Возраст разный → разные выставочные классы.
                dob = date(2024 - k, 5 + k, 10 + k)
                pup, _ = await _get_or_create(
                    db, Dog, {"rkf_number": rkf},
                    {
                        "breed_id": breed.id,
                        "name": name,
                        "sex": sex,
                        "date_of_birth": dob,
                        "color": colors[(bi + k) % len(colors)],
                        "rkf_number": rkf,
                        "tattoo": f"D{rkf_counter:03d}",
                        "microchip": f"64309410099{rkf_counter:04d}",
                        "breeder_kennel_id": kennel.id,
                        "kennel_id": kennel.id,
                        "father_id": sire.id,
                        "mother_id": dam.id,
                        "description": "Выставочная перспектива, "
                                       "социализирован, привит по возрасту.",
                    },
                )
                pup.owner_id = breeder_u.id
                owner_of[pup.id] = breeder_u.id
                dogs_by_breed.setdefault(breed_idx, []).append(pup)
                pups_by_pair.setdefault((bi, breed_idx), []).append(pup)
                # У щенков — галерея из 2 фото (главное + второе).
                dog_specs.append((pup, breeder_u.id, 2))

    # 3.1 «Проданный» щенок: владелец — buyer1, текущего питомника нет,
    #     питомник-заводчик остаётся. Даёт buyer1 непустые «Мои собаки».
    sold_pup = dogs_by_breed[0][1]
    sold_pup.owner_id = buyer1.id
    sold_pup.kennel_id = None
    owner_of[sold_pup.id] = buyer1.id

    # 3.2 Молодняк для классов бэби/щенков/юниоров. Даты рождения держим
    #     относительно TODAY — иначе со временем все «повзрослеют».
    young: dict[str, dict[int, Dog]] = {"junior": {}, "puppy": {}}
    young_spec = [
        # (вид, breed_idx, кличка, возраст в днях, пол)
        ("junior", 0, "ОРИОН", 380, SexEnum.male),
        ("junior", 1, "ВЕСНА", 400, SexEnum.female),
        ("junior", 2, "АРГО", 390, SexEnum.male),
        ("puppy", 0, "БУСИНКА", 230, SexEnum.female),
        ("puppy", 4, "ФУНТИК", 220, SexEnum.male),
        ("baby", 4, "ПУГОВКА", 150, SexEnum.female),
    ]
    for kind, bidx, first, age_days, sex in young_spec:
        sire, dam, breeder_u, kennel = parents_by_breed[bidx]
        rkf = f"RKF-DEMO-{kind.upper()}-{bidx}"
        d, _ = await _upsert(
            db, Dog, {"rkf_number": rkf},
            {
                "breed_id": breeds[bidx].id,
                "name": f"{first} {kennel.kennel_prefix}",
                "sex": sex,
                "color": colors[bidx % len(colors)],
                "rkf_number": rkf,
                "breeder_kennel_id": kennel.id,
                "kennel_id": kennel.id,
                "father_id": sire.id,
                "mother_id": dam.id,
            },
            {
                "date_of_birth": TODAY - timedelta(days=age_days),
                "owner_id": breeder_u.id,
            },
        )
        owner_of[d.id] = breeder_u.id
        young.setdefault(kind, {})[bidx] = d
        dog_specs.append((d, breeder_u.id, 1))

    # 3.3 Производительницы для «запланированного» и «архивного» помётов.
    extra_dams = [
        # (bi, breed_idx, кличка, rkf)
        (1, 5, "ВЕСТА", "RKF-DEMO-DAM-X-1-5"),
        (3, 1, "ИРМА", "RKF-DEMO-DAM-X-3-1"),
    ]
    extra_dam_by_key: dict[tuple[int, int], Dog] = {}
    for bi, bidx, first, rkf in extra_dams:
        breeder_u, kennel = breeders[bi]
        d, _ = await _get_or_create(
            db, Dog, {"rkf_number": rkf},
            {
                "breed_id": breeds[bidx].id,
                "name": f"{first} {kennel.kennel_prefix}",
                "sex": SexEnum.female,
                "date_of_birth": date(2021, 2, 14),
                "color": colors[(bidx + 3) % len(colors)],
                "rkf_number": rkf,
                "breeder_kennel_id": kennel.id,
                "kennel_id": kennel.id,
            },
        )
        d.owner_id = breeder_u.id
        extra_dam_by_key[(bi, bidx)] = d
        dog_specs.append((d, breeder_u.id, 1))

    # 3.5 Демо-фото, аватары и превью. Единственный «инфраструктурный»
    #     шаг сида — если MinIO не поднят, даём внятную подсказку.
    try:
        n_photos = 0
        for dog_obj, owner_id, want in dog_specs:
            n_photos += await _attach_photos(db, dog_obj, owner_id, want)
        for u, k in breeders:
            await _ensure_avatar(db, k, u.id, k.name)
            await _ensure_avatar(db, u, u.id, u.email)
        for u in (organizer, buyer1, admin):
            await _ensure_avatar(db, u, u.id, u.email)
        n_variants = await _backfill_variants(db)
    except (ClientError, EndpointConnectionError) as e:
        raise SystemExit(
            "MinIO/S3 недоступен — демо-файлы не загружены. Подними "
            "хранилище (docker compose up -d minio) и запусти сид заново. "
            f"Детали: {e}"
        )
    logger.info("Демо-фото загружено: %d, превью досоздано: %d",
                n_photos, n_variants)

    # 4. Помёты: по одному на каждую (питомник × порода) пару. Ссылаемся на
    #    реальных производителей и проставляем litter_id всем щенкам этой
    #    пары (включая проданного — помёт определяется заводчиком, а не
    #    текущим владельцем).
    litter_statuses = [
        LitterStatus.available, LitterStatus.born, LitterStatus.sold_out,
    ]
    litter_by_pair: dict[tuple[int, int], Litter] = {}
    li = 0
    for bi, (breeder_u, kennel) in enumerate(breeders):
        for breed_idx in (bi % len(breeds), (bi + 2) % len(breeds)):
            breed = breeds[breed_idx]
            # Пара производителей именно этого питомника (parents_by_breed
            # хранит только последнюю пару породы).
            pups = pups_by_pair[(bi, breed_idx)]
            sire_id, dam_id = pups[0].father_id, pups[0].mother_id
            males = sum(1 for p in pups if p.sex == SexEnum.male)
            status = litter_statuses[li % len(litter_statuses)]
            li += 1
            litter, _ = await _upsert(
                db, Litter,
                # Натуральный ключ помёта: питомник + порода.
                {"kennel_id": kennel.id, "breed_id": breed.id},
                {
                    "kennel_id": kennel.id,
                    "breed_id": breed.id,
                    "father_id": sire_id,
                    "mother_id": dam_id,
                    "puppies_count": len(pups),
                    "males_count": males,
                    "females_count": len(pups) - males,
                    "price_from": Decimal("40000.00"),
                    "price_to": Decimal("90000.00"),
                    "status": status,
                    "description": f"Помёт питомника «{kennel.name}», "
                                   f"порода {breed.name}. Родители с титулами, "
                                   "актированы, есть документы РКФ.",
                },
                {"born_at": TODAY - timedelta(days=40 + li * 10)},
            )
            litter_by_pair[(bi, breed_idx)] = litter
            for p in pups:
                p.litter_id = litter.id

    # 4.1 Запланированная вязка (кобель из другого питомника) и архивный
    #     помёт — статусы planned/archived без привязанных щенков.
    stud_5 = parents_by_breed[5][0]
    await _upsert(
        db, Litter,
        {"kennel_id": breeders[1][1].id, "breed_id": breeds[5].id},
        {
            "kennel_id": breeders[1][1].id,
            "breed_id": breeds[5].id,
            "father_id": stud_5.id,
            "mother_id": extra_dam_by_key[(1, 5)].id,
            "price_from": Decimal("60000.00"),
            "status": LitterStatus.planned,
            "description": "Планируется вязка с кобелем питомника "
                           "«Верный Друг». Принимаем предварительные заявки.",
        },
        {"born_at": None},
    )
    stud_1 = pups_by_pair[(1, 1)][0].father_id
    await _upsert(
        db, Litter,
        {"kennel_id": breeders[3][1].id, "breed_id": breeds[1].id},
        {
            "kennel_id": breeders[3][1].id,
            "breed_id": breeds[1].id,
            "father_id": stud_1,
            "mother_id": extra_dam_by_key[(3, 1)].id,
            "puppies_count": 5, "males_count": 2, "females_count": 3,
            "status": LitterStatus.archived,
            "description": "Архивный помёт: все щенки давно в новых семьях.",
        },
        {"born_at": date(2022, 8, 20)},
    )

    # 5. Объявления — по всем категориям, видам цены, статусам и
    #    доступностям. (автор, категория, вид цены, цена, порода, город,
    #    заголовок, описание, пол, доступность, статус, помёт, фото)
    arkadia_litter = litter_by_pair[(0, 0)]
    classifieds_spec = [
        (buyer1, ClassifiedCategory.puppy_sale, ClassifiedPriceKind.fixed,
         Decimal("65000.00"), breeds[0], "Москва",
         "Щенки на продажу — шоу-класс",
         "Продаются щенки от титулованных родителей. Привиты, "
         "клеймо, документы РКФ. Возможна доставка.",
         None, AnimalAvailability.reserved, ClassifiedStatus.active, None, 2),
        (breeders[0][0], ClassifiedCategory.adult_sale,
         ClassifiedPriceKind.negotiable, None, breeds[1], "Санкт-Петербург",
         "Взрослая собака в шоу-дом",
         "Перспективная сука, юный чемпион. Цена договорная для "
         "выставочного дома с амбициями.",
         SexEnum.female, AnimalAvailability.available,
         ClassifiedStatus.active, None, 2),
        (breeders[1][0], ClassifiedCategory.mating, ClassifiedPriceKind.fixed,
         Decimal("30000.00"), breeds[2], "Казань",
         "Вязка с интерчемпионом",
         "Предлагается кобель-производитель, интерчемпион, "
         "отличные тесты здоровья. Алименты или оплата.",
         SexEnum.male, AnimalAvailability.available,
         ClassifiedStatus.active, None, 1),
        (breeders[2][0], ClassifiedCategory.handler,
         ClassifiedPriceKind.negotiable, None, None, "Москва",
         "Услуги хендлера на выставках",
         "Опытный хендлер. Подготовка и показ в ринге, "
         "выставки любого ранга по РФ.",
         None, AnimalAvailability.available, ClassifiedStatus.active, None, 0),
        (breeders[3][0], ClassifiedCategory.grooming,
         ClassifiedPriceKind.fixed, Decimal("3500.00"), None, "Екатеринбург",
         "Груминг выставочных собак",
         "Профессиональный груминг к выставке: тримминг, "
         "стрижка, подготовка шерсти.",
         None, AnimalAvailability.available, ClassifiedStatus.active, None, 1),
        (buyer2, ClassifiedCategory.puppy_sale, ClassifiedPriceKind.free,
         None, breeds[3], "Казань",
         "Щенок в добрые руки",
         "Метис без документов ищет ответственных хозяев. "
         "Отдаётся бесплатно, привит.",
         SexEnum.male, AnimalAvailability.available,
         ClassifiedStatus.active, None, 1),
        (breeders[0][0], ClassifiedCategory.other,
         ClassifiedPriceKind.negotiable, None, None, "Москва",
         "Передержка и выгул",
         "Передержка собак на время отпуска владельцев. "
         "Домашние условия, опыт работы с шоу-собаками.",
         None, AnimalAvailability.available, ClassifiedStatus.active, None, 0),
        # Объявление от помёта (litter_id) — щенки обоих полов → sex=NULL.
        (breeders[0][0], ClassifiedCategory.puppy_sale,
         ClassifiedPriceKind.fixed, Decimal("80000.00"), breeds[0], "Москва",
         "Щенки из помёта питомника «Аркадия» (демо)",
         "Помёт от чемпионов РКФ, щенки с метрикой и клеймом. "
         "Родители на фото, можно приехать познакомиться.",
         None, AnimalAvailability.available, ClassifiedStatus.active,
         arkadia_litter, 2),
        # Закрытое: животное «пристроено» (free + sold).
        (buyer2, ClassifiedCategory.puppy_sale, ClassifiedPriceKind.free,
         None, None, "Москва",
         "Щенок пристроен (демо)",
         "Спасибо всем откликнувшимся — щенок пристроен в семью.",
         SexEnum.female, AnimalAvailability.sold, ClassifiedStatus.closed,
         None, 1),
        # Архивное.
        (breeders[1][0], ClassifiedCategory.adult_sale,
         ClassifiedPriceKind.fixed, Decimal("120000.00"), breeds[3],
         "Санкт-Петербург",
         "Продан: кобель-чемпион (архив, демо)",
         "Объявление в архиве — собака продана в прошлом сезоне.",
         SexEnum.male, AnimalAvailability.sold, ClassifiedStatus.archived,
         None, 1),
        # «На модерации» — чтобы список GET /admin/moderation/classifieds
        # был не пустой и модерацию можно было пройти с фронта.
        (buyer2, ClassifiedCategory.puppy_sale, ClassifiedPriceKind.fixed,
         Decimal("55000.00"), breeds[4], "Нижний Новгород",
         "Щенки на модерации (демо)",
         "Новое объявление, ожидает проверки модератором. "
         "Появится в публичной выдаче после одобрения.",
         None, AnimalAvailability.available, ClassifiedStatus.moderation,
         None, 1),
    ]
    classified_objs: list[tuple[Classified, int]] = []
    for (author, category, price_kind, price, breed, city, title, descr,
         sex, availability, status, litter_obj, n_images) in classifieds_spec:
        cl, _ = await _upsert(
            db, Classified, {"title": title},
            {
                "author_id": author.id,
                "category": category,
                "breed_id": breed.id if breed else None,
                "title": title,
                "description": descr,
                "price": price,
                "price_kind": price_kind,
                "city": city,
                "contact_phone": "+7 (900) 000-00-00",
                "contact_email": author.email,
                "views_count": 37,
            },
            {
                "sex": sex,
                "availability": availability,
                "status": status,
                "litter_id": litter_obj.id if litter_obj else None,
            },
        )
        classified_objs.append((cl, n_images))
    try:
        for cl, n_images in classified_objs:
            await _attach_classified_images(db, cl, n_images)
    except (ClientError, EndpointConnectionError) as e:
        raise SystemExit(f"MinIO/S3 недоступен: {e}")

    # 6. Реклама — кампании во всех статусах, баннеры во всех местах,
    #    события показов/кликов (статистика дашборда).
    campaigns_spec = [
        # (рекламодатель, имя, статус, начало, конец, бюджет, баннеры)
        (breeders[0][0], "Корм PremiumDog — весна 2026 (демо)",
         CampaignStatus.active, TODAY - timedelta(days=10),
         TODAY + timedelta(days=80), Decimal("50000.00"),
         [(BannerPlacement.top, "PremiumDog — скидка 20% на первый заказ"),
          (BannerPlacement.sidebar, "Корм для шоу-собак PremiumDog"),
          (BannerPlacement.inline, "Витамины для выставочной шерсти"),
          (BannerPlacement.footer, "PremiumDog — официальный партнёр")]),
        (breeders[1][0], "Амуниция ShowLead (черновик, демо)",
         CampaignStatus.draft, TODAY + timedelta(days=15),
         TODAY + timedelta(days=60), Decimal("20000.00"),
         [(BannerPlacement.sidebar, "Ринговки ShowLead ручной работы")]),
        (breeders[2][0], "Груминг-салон «Шик» (пауза, демо)",
         CampaignStatus.paused, TODAY - timedelta(days=20),
         TODAY + timedelta(days=40), Decimal("15000.00"),
         [(BannerPlacement.inline, "Подготовка к выставке за 1 день")]),
        (breeders[3][0], "Ветклиника «Айболит» — зима (демо)",
         CampaignStatus.completed, TODAY - timedelta(days=120),
         TODAY - timedelta(days=30), Decimal("10000.00"),
         [(BannerPlacement.top, "Вакцинация щенков со скидкой")]),
        (buyer1, "Зоотакси (отменена, демо)",
         CampaignStatus.cancelled, TODAY - timedelta(days=5),
         TODAY + timedelta(days=25), Decimal("5000.00"),
         [(BannerPlacement.footer, "Перевозка собак на выставки")]),
    ]
    n_ad_events = 0
    for (advertiser, cname, cstatus, dstart, dend, budget,
         banners_spec) in campaigns_spec:
        campaign, _ = await _upsert(
            db, AdCampaign, {"name": cname},
            {
                "advertiser_id": advertiser.id,
                "name": cname,
                "description": "Демо-кампания для проверки кабинета рекламы.",
                "budget": budget,
                "cost_per_impression": Decimal("0.50"),
            },
            {
                "date_start": dstart,
                "date_end": dend,
                "status": cstatus,
                # Демо-кампании, побывавшие в показе, считаем одобренными
                # модератором (ревью 2026-10-06, BE-05) — иначе владелец не
                # сможет вернуть кампанию в показ после паузы.
                "approved_at": (
                    None if cstatus == CampaignStatus.draft
                    else datetime.now(timezone.utc)
                ),
            },
        )
        spent = Decimal("0")
        for bi_, (placement, btitle) in enumerate(banners_spec):
            banner, _ = await _upsert(
                db, AdBanner,
                {"campaign_id": campaign.id, "title": btitle},
                {
                    "campaign_id": campaign.id,
                    "target_url": "https://example.com/promo",
                    "title": btitle,
                    "placement": placement,
                },
                {
                    # Таргетинг по породе/региону — у одного баннера.
                    "target_breed_id": breeds[0].id if bi_ == 1 else None,
                    "target_region": "Москва" if bi_ == 2 else None,
                    "is_active": cstatus == CampaignStatus.active,
                },
            )
            if banner.image_file_id is None:
                img = await _upload_image(
                    db, owner_id=advertiser.id, label=btitle,
                    color=_color_for(banner.id), folder="ads",
                    filename="banner.jpg", size=(728, 180),
                )
                banner.image_file_id = img.id
            # События — только у кампаний, которые реально крутились.
            if cstatus in (CampaignStatus.active, CampaignStatus.paused,
                           CampaignStatus.completed):
                n_ad_events += await _seed_ad_events(
                    db, banner, dstart, [buyer1, buyer2, None]
                )
            banner.impressions_count = await _count(
                db, AdEvent, AdEvent.banner_id == banner.id,
                AdEvent.event_type == AdEventType.impression,
            )
            banner.clicks_count = await _count(
                db, AdEvent, AdEvent.banner_id == banner.id,
                AdEvent.event_type == AdEventType.click,
            )
            spent += banner.impressions_count * campaign.cost_per_impression
        campaign.spent = min(spent, campaign.budget)

    # 7. Выставки во всех статусах. Даты — относительно TODAY и
    #    обновляются при каждом запуске.
    # (имя, статус, ранг, начало, дней, город, площадка, allow-list пород)
    shows_spec = [
        ("Зимний кубок РКФ — 2026 (демо)", ShowStatus.completed, "cac-chrkf",
         TODAY - timedelta(days=60), 1, "Москва", "Крокус Экспо", []),
        (SHOW_COMPLETED, ShowStatus.completed, "cac-chf",
         TODAY - timedelta(days=14), 2, "Москва", "ВДНХ, павильон 75",
         breeds[:3]),
        ("Фестиваль породы (демо)", ShowStatus.in_progress, "cacib",
         TODAY, 2, "Москва", "Сокольники", []),
        ("Кубок Поволжья (демо)", ShowStatus.registration_closed, "kchk",
         TODAY + timedelta(days=5), 1, "Нижний Новгород", "Нижегородская ярмарка",
         []),
        ("Весенняя выставка ЧФ (демо)", ShowStatus.registration_open,
         "cac-chf", TODAY + timedelta(days=30), 1, "Санкт-Петербург",
         "Экспофорум", [breeds[0], breeds[1], breeds[3]]),
        ("Летний CACIB (демо)", ShowStatus.registration_open, "cacib",
         TODAY + timedelta(days=75), 2, "Казань", "Казань Экспо", []),
        ("Осенний национальный показ (демо)", ShowStatus.draft, "cac-chf",
         TODAY + timedelta(days=120), 1, "Екатеринбург", "Екатеринбург-ЭКСПО",
         []),
        ("Монопородная (отменена) (демо)", ShowStatus.cancelled,
         "monoporodnaya", TODAY + timedelta(days=20), 1, "Москва",
         "Сокольники", [breeds[0]]),
    ]
    shows: dict[str, Show] = {}
    for name, status, rank_code, dstart, days, city, venue, allow in shows_spec:
        deadline = (
            dstart - timedelta(days=2)
            if status == ShowStatus.registration_closed
            else dstart - timedelta(days=7)
        )
        s, _ = await _upsert(
            db, Show, {"name": name},
            {
                "organizer_id": organizer.id,
                "name": name,
                "description": "Сертификатная выставка. Запись онлайн, "
                               "ринги по группам FCI, эксперты РКФ.",
                "city": city,
                "country": "Россия",
                "venue": venue,
                "entry_fee": Decimal("2500.00"),
            },
            {
                "rank_id": ranks[rank_code].id,
                "status": status,
                "date_start": dstart,
                "date_end": dstart + timedelta(days=days - 1) if days > 1 else None,
                "registration_deadline": deadline,
            },
        )
        await _set_show_breeds(db, s, allow)
        shows[name] = s
    await db.flush()

    def spec(d: Dog, **kw) -> EntrySpec:
        return EntrySpec(dog=d, owner_id=owner_of[d.id], **kw)

    veteran_sire = parents_by_breed[0][0]
    # Завершённая выставка: полный граф + BIG/BIS + судья на группу.
    completed_entries = [
        spec(d) for idx in (0, 1, 2) for d in dogs_by_breed[idx]
    ] + [
        spec(young["junior"][0]), spec(young["junior"][1]),
        spec(young["junior"][2]), spec(young["puppy"][0]),
        spec(veteran_sire, class_code="veteran"),
    ]
    # Хендлер у одной записи, «Отсутствует» — у последней собаки ринга 3.
    completed_entries[0].handler_id = handler_user.id
    completed_entries[0].notes = "В ринге показывает хендлер."
    completed_entries[len(dogs_by_breed[0]) + len(dogs_by_breed[1])
                      + len(dogs_by_breed[2]) - 1].absent = True
    await _build_show_graph(
        db, shows[SHOW_COMPLETED], rank=ranks["cac-chf"],
        rings=[
            RingSpec(breeds[0], judge1, time(10, 0)),
            RingSpec(breeds[1], judge2, time(10, 0)),
            RingSpec(breeds[2], judge1, time(13, 0)),
        ],
        entries=completed_entries, refs=refs, assign_catalog=True,
        result_breed_ids={b.id for b in breeds[:3]},
        big_bis=True, group_judge=judge2,
    )
    # Прошлая завершённая выставка — у собак титулы с двух выставок.
    await _build_show_graph(
        db, shows["Зимний кубок РКФ — 2026 (демо)"], rank=ranks["cac-chrkf"],
        rings=[
            RingSpec(breeds[3], judge2, time(10, 0)),
            RingSpec(breeds[0], judge1, time(11, 0)),
        ],
        entries=[spec(d) for d in dogs_by_breed[3]]
        + [spec(d) for d in dogs_by_breed[0]],
        refs=refs, assign_catalog=True,
        result_breed_ids={breeds[3].id, breeds[0].id}, big_bis=True,
    )
    # Идёт сейчас: CACIB, результаты только по первому рингу.
    await _build_show_graph(
        db, shows["Фестиваль породы (демо)"], rank=ranks["cacib"],
        rings=[
            RingSpec(breeds[0], judge1, time(10, 0)),
            RingSpec(breeds[1], judge2, time(12, 0)),
        ],
        entries=[spec(d) for d in dogs_by_breed[0]]
        + [spec(d) for d in dogs_by_breed[1]]
        + [spec(young["junior"][1])],
        refs=refs, assign_catalog=True, result_breed_ids={breeds[0].id},
    )
    # Регистрация закрыта: номера каталога есть, результатов нет.
    await _build_show_graph(
        db, shows["Кубок Поволжья (демо)"], rank=ranks["kchk"],
        rings=[
            RingSpec(breeds[4], judge1, time(10, 0)),
            RingSpec(breeds[5], judge2, time(10, 0)),
        ],
        entries=[spec(d) for d in dogs_by_breed[4][:3]]
        + [spec(d) for d in dogs_by_breed[5][:2]]
        + [spec(young["puppy"][4]), spec(young["baby"][4])],
        refs=refs, assign_catalog=True, result_breed_ids=set(),
    )
    # Регистрация открыта: записи без номеров каталога, запись buyer1
    # с хендлером.
    await _build_show_graph(
        db, shows["Весенняя выставка ЧФ (демо)"], rank=ranks["cac-chf"],
        rings=[
            RingSpec(breeds[0], judge1, time(10, 0)),
            RingSpec(breeds[1], judge2, time(10, 0)),
            RingSpec(breeds[3], judge1, time(13, 0)),
        ],
        entries=[
            spec(sold_pup, handler_id=handler_user.id,
                 notes="Первая выставка, нужна помощь хендлера."),
            spec(young["junior"][1]),
            spec(dogs_by_breed[3][0]),
            spec(dogs_by_breed[3][1]),
        ],
        refs=refs, assign_catalog=False, result_breed_ids=set(),
    )

    now = datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0, tzinfo=timezone.utc)

    # 9. Блог: два опубликованных поста и один черновик (для проверки фильтра
    #    publish и доступа writer'а к черновику). slug задаём явно — сид
    #    идемпотентен по нему, не дёргаем генератор slug из сервиса.
    posts_spec = [
        ("kak-vybrat-shchenka-demo", "Как выбрать щенка шоу-класса",
         PostPublish.published, organizer,
         ["щенки", "выбор", "шоу"],
         "<p>Породный тип, движения и темперамент — три кита оценки "
         "перспективного щенка. Смотрите на родителей и тесты здоровья.</p>"),
        ("podgotovka-k-vystavke-demo", "Подготовка собаки к выставке",
         PostPublish.published, admin,
         ["выставка", "хендлинг", "груминг"],
         "<p>За месяц до ринга: кондиция, шерсть, отработка стойки и "
         "движения по рингу. Чек-лист дня выставки внутри.</p>"),
        ("chernovik-novosti-demo", "Черновик: новости платформы",
         PostPublish.draft, admin,
         ["новости"],
         "<p>Неопубликованный черновик — виден только admin/organizer.</p>"),
    ]
    for slug, title, publish, author, tags, content in posts_spec:
        p, _ = await _get_or_create(
            db, Post, {"slug": slug},
            {
                "title": title,
                "slug": slug,
                "description": title + ". Демо-материал блога Show Ring.",
                "content": content,
                "tags": tags,
                "meta_keywords": tags,
                "meta_title": title,
                "meta_description": title + " — практическое руководство.",
                "publish": publish,
                "author_id": author.id,
                # Денормализованные счётчики — чтобы карточки блога были «живыми».
                "total_views": 120, "total_shares": 4,
                "total_comments": 0, "total_favorites": 7,
            },
        )
        # Обложка — у опубликованных постов; у черновика пусто.
        if publish == PostPublish.published and not p.cover_url:
            cover = await _upload_image(
                db, owner_id=author.id, label=title, color=_color_for(p.id),
                folder="posts", filename="cover.jpg", size=(1200, 630),
            )
            p.cover_url = f"{FILES_BASE_URL}/files/{cover.id}"

    # 10. Подписки на события (GET /subscriptions). Натуральный ключ —
    #     UNIQUE (user, event, breed, region, channel), по нему и идемпотентим.
    subs_spec = [
        (buyer1, EventType.LITTER_ANNOUNCED, breeds[0].id, None,
         NotificationChannel.email),
        (buyer1, EventType.SHOW_RESULTS_PUBLISHED, None, None,
         NotificationChannel.in_app),
        (buyer2, EventType.LITTER_ANNOUNCED, None, "Москва",
         NotificationChannel.email),
        (buyer2, EventType.DOG_TITLE_EARNED, None, None,
         NotificationChannel.email),
    ]
    for sub_user, event, breed_id, region, channel in subs_spec:
        await _get_or_create(
            db, Subscription,
            {
                "user_id": sub_user.id,
                "event_type": event.value,
                "filter_breed_id": breed_id,
                "filter_region": region,
                "channel": channel,
            },
            {
                "user_id": sub_user.id,
                "event_type": event.value,
                "filter_breed_id": breed_id,
                "filter_region": region,
                "channel": channel,
                "is_active": True,
            },
        )

    # 11. Уведомления (GET /notifications, бейдж непрочитанных). Разные каналы
    #     и статусы: in_app непрочитанное/прочитанное, email отправленное,
    #     pending и failed. message_id детерминированный → идемпотентность.
    notif_spec = [
        (NotificationChannel.in_app, EventType.SHOW_RESULTS_PUBLISHED.value,
         "Опубликованы результаты выставки «Кубок столицы — 2026»",
         NotificationStatus.sent, None, False),  # непрочитанное в ленте
        (NotificationChannel.in_app, EventType.LITTER_ANNOUNCED.value,
         "Новый помёт в питомнике «Аркадия»",
         NotificationStatus.sent, None, True),    # прочитанное в ленте
        (NotificationChannel.email, EventType.LITTER_ANNOUNCED.value,
         "Доступен новый помёт по вашей подписке",
         NotificationStatus.sent, None, True),
        (NotificationChannel.email, EventType.DOG_TITLE_EARNED.value,
         "Собака получила новый титул",
         NotificationStatus.pending, None, False),
        (NotificationChannel.email, EventType.SHOW_REGISTRATION_OPENED.value,
         "Открыта регистрация на «Летний CACIB»",
         NotificationStatus.failed, "SMTP timeout", False),
    ]
    for i, (channel, event, subject, nstatus, error, is_read) in enumerate(
        notif_spec
    ):
        mid = uuid.uuid5(uuid.NAMESPACE_URL, f"demo-notif:{buyer1.id}:{i}")
        await _get_or_create(
            db, Notification, {"message_id": mid},
            {
                "message_id": mid,
                "user_id": buyer1.id,
                "event_type": event,
                "channel": channel,
                "subject": subject,
                "status": nstatus,
                "error": error,
                "sent_at": (
                    now - timedelta(hours=i)
                    if nstatus == NotificationStatus.sent else None
                ),
                "read_at": now - timedelta(hours=i) if is_read else None,
            },
        )

    # 12. Поддержка: тикеты во всех статусах. Натуральный ключ тикета —
    #     (user_id, subject); переписка создаётся только вместе с тикетом.
    # (автор, тема, статус, приоритет, оператор, [(от оператора?, прочит., текст)])
    tickets_spec = [
        (buyer1, "Не приходит письмо-подтверждение", TicketStatus.in_progress,
         TicketPriority.high, operator,
         [(False, True, "Здравствуйте! Зарегистрировался, но письмо для "
           "подтверждения email так и не пришло. Подскажите, что делать?"),
          (True, False, "Здравствуйте! Проверьте папку «Спам». Я повторно "
           "отправила письмо на ваш адрес — оно должно прийти в течение "
           "5 минут.")]),
        (buyer2, "Как опубликовать объявление?", TicketStatus.resolved,
         TicketPriority.normal, operator,
         [(False, True, "Подскажите, как разместить объявление о продаже "
           "щенка?"),
          (True, True, "В разделе «Объявления» нажмите «Создать». После "
           "модерации оно появится в публичной выдаче. Хорошего дня!")]),
        (breeders[0][0], "Не могу добавить фото питомника",
         TicketStatus.open, TicketPriority.low, None,
         [(False, False, "При загрузке аватара питомника крутится "
           "индикатор и ничего не происходит.")]),
        (breeders[1][0], "Ошибка в результатах выставки",
         TicketStatus.closed, TicketPriority.urgent, operator,
         [(False, True, "В результатах перепутан класс у моей собаки."),
          (True, True, "Организатор исправил запись, результаты "
           "пересчитаны. Закрываю обращение.")]),
    ]
    for author, subject, tstatus, prio, assignee, messages in tickets_spec:
        ticket, created = await _get_or_create(
            db, SupportTicket, {"user_id": author.id, "subject": subject},
            {
                "user_id": author.id,
                "subject": subject,
                "status": tstatus,
                "priority": prio,
                "assigned_to_id": assignee.id if assignee else None,
            },
        )
        if created:
            for from_op, is_read, body in messages:
                db.add(SupportMessage(
                    ticket_id=ticket.id,
                    sender_id=(assignee or operator).id if from_op else author.id,
                    is_from_operator=from_op, is_read=is_read, body=body,
                ))

    # 13. Лог модерации (admin видит историю решений). Полиморфные target —
    #     питомники (верификация) и объявления. Идемпотентны по
    #     (actor, action, target_id).
    verified_kennels = (await db.execute(
        select(Kennel).where(Kennel.is_verified.is_(True))
    )).scalars().all()
    modlog_spec = [
        ("kennel.verify", "kennel", k.id,
         "Документы РКФ проверены, питомник подтверждён.",
         {"prev": False, "new": True})
        for k in verified_kennels
    ]
    for cl, _n in classified_objs:
        if cl.status == ClassifiedStatus.archived:
            modlog_spec.append((
                "classified.archive", "classified", cl.id,
                "Объявление старше 6 месяцев, перенесено в архив.",
                {"prev": "active", "new": "archived"},
            ))
    for action, ttype, tid, reason, extra in modlog_spec:
        await _get_or_create(
            db, ModerationLog,
            {"actor_id": admin.id, "action": action, "target_id": tid},
            {
                "actor_id": admin.id, "action": action, "target_type": ttype,
                "target_id": tid, "reason": reason, "extra": extra,
            },
        )

    # 14. Журнал безопасности аккаунта (GET истории операций над собой).
    sec_spec = [
        ("password_changed", {"method": "self_service"}),
        ("email_change_requested",
         {"old_email": buyer1.email, "new_email": "new-buyer1@dogshow.ru"}),
    ]
    for action, extra in sec_spec:
        await _get_or_create(
            db, SecurityAuditLog,
            {"user_id": buyer1.id, "action": action},
            {
                "user_id": buyer1.id,
                "action": action,
                "ip": "203.0.113.10",
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "ShowRing-Demo",
                "extra": extra,
            },
        )

    # 15. Фоновые задачи (GET /tasks/{id}, /tasks/{id}/download). Завершённая
    #     ссылается на реальный приватный PDF в MinIO, вторая — в очереди.
    catalog_show = shows[SHOW_COMPLETED]
    task_done, _ = await _get_or_create(
        db, Task,
        {"type": "generate_catalog", "created_by": organizer.id},
        {
            "type": "generate_catalog",
            "status": TaskStatusEnum.done,
            "payload": {"show_id": str(catalog_show.id)},
            "created_by": organizer.id,
            "attempts": 1,
        },
    )
    result_file_id = (task_done.result or {}).get("file_id")
    has_file = result_file_id and await db.get(
        UploadedFile, uuid.UUID(str(result_file_id))
    )
    if not has_file:
        pdf = _make_pdf_bytes([
            "Show Ring - demo catalog",
            f"Show id: {catalog_show.id}",
            f"Date: {catalog_show.date_start.isoformat()}",
        ])
        s3_key, nbytes = await file_storage.upload_bytes(
            pdf, content_type="application/pdf", extension="pdf",
            folder="documents",
        )
        pdf_file = UploadedFile(
            uploaded_by=organizer.id, s3_key=s3_key,
            original_filename="catalog-demo.pdf",
            content_type="application/pdf", size_bytes=nbytes,
            # Документы с ПДн приватны — как у воркера (см. UploadedFile).
            is_public=False,
        )
        db.add(pdf_file)
        await db.flush()
        task_done.result = {"file_id": str(pdf_file.id)}
    task_done.payload = {"show_id": str(catalog_show.id)}
    await _get_or_create(
        db, Task,
        {"type": "generate_diploma", "created_by": organizer.id},
        {
            "type": "generate_diploma",
            "status": TaskStatusEnum.pending,
            "payload": {"show_id": str(catalog_show.id)},
            "result": None,
            "created_by": organizer.id,
            "attempts": 0,
        },
    )

    # 16. Тиры квот загрузки. Обычно засеяны миграцией; страхуемся, чтобы
    #     GET /admin/upload-quotas всегда отдавал три строки.
    quota_defaults = [
        ("untrusted", 5, 50 * 1024 * 1024),
        ("standard", 50, 500 * 1024 * 1024),
        ("breeder", 200, 5 * 1024 * 1024 * 1024),
    ]
    for tier, daily, storage in quota_defaults:
        await _get_or_create(
            db, UploadQuotaTier, {"tier": tier},
            {"tier": tier, "daily_limit": daily, "max_storage_bytes": storage},
        )

    logger.info("События рекламы добавлено: %d", n_ad_events)
    await db.commit()
    await _print_summary(db)


async def _seed_ad_events(
    db: AsyncSession, banner: AdBanner, since: date, users: list[User | None]
) -> int:
    """~10 показов/день за 10 дней и ~5% кликов. Только если событий нет."""
    if await _count(db, AdEvent, AdEvent.banner_id == banner.id):
        return 0
    start = datetime(since.year, since.month, since.day, 9, tzinfo=timezone.utc)
    n = 0
    for day in range(10):
        for j in range(10 + (banner.id.int + day) % 6):
            u = users[j % len(users)]
            ts = start + timedelta(days=day, minutes=37 * j)
            common = {
                "banner_id": banner.id,
                "user_id": u.id if u else None,
                "ip": f"198.51.100.{(j * 7 + day) % 250 + 1}",
                "user_agent_hash": hashlib.sha256(
                    f"demo-ua-{j % 4}".encode()
                ).hexdigest(),
                "page_url": "/shows",
                "created_at": ts,
            }
            db.add(AdEvent(event_type=AdEventType.impression, **common))
            n += 1
            if j % 20 == 3:
                db.add(AdEvent(
                    event_type=AdEventType.click,
                    **{**common, "created_at": ts + timedelta(seconds=5)},
                ))
                n += 1
    await db.flush()
    return n


async def _print_summary(db: AsyncSession) -> None:
    counts = [
        ("выставок", Show), ("записей", ShowEntry), ("результатов", ShowResult),
        ("титулов собак", DogTitle), ("собак", Dog), ("фото собак", DogPhoto),
        ("файлов", UploadedFile), ("превью", FileVariant),
        ("питомников", Kennel), ("помётов", Litter), ("объявлений", Classified),
        ("фото объявл.", ClassifiedImage), ("баннеров", AdBanner),
        ("событий рекл.", AdEvent), ("постов", Post), ("подписок", Subscription),
        ("уведомлений", Notification), ("тикетов", SupportTicket),
        ("лог модер.", ModerationLog), ("задач", Task),
    ]
    print("\n" + "=" * 60)
    print("ДЕМО-СИД ГОТОВ")
    for label, model in counts:
        print(f"  {label + ':':15}{await _count(db, model)}")
    print(f"  логины (пароль у всех {DEMO_PASSWORD}):")
    print("    admin-demo@dogshow.ru      — администратор")
    print("    operator-demo@dogshow.ru   — оператор поддержки")
    print("    org-demo@dogshow.ru        — организатор")
    print("    judge1-demo@dogshow.ru     — судья")
    print("    breeder1-demo@dogshow.ru   — заводчик")
    print("    buyer1-demo@dogshow.ru     — покупатель (есть своя собака)")
    print("=" * 60)


async def main() -> None:
    # Предохранитель (review 2026-06-10): сид создаёт активных
    # пользователей с is_email_verified=True и общеизвестным паролем —
    # запуск с прод-DATABASE_URL (ошибка оператора) дал бы набор
    # бэкдор-аккаунтов. Работаем только при settings.debug=True; на
    # проде нужен явный флаг --force.
    from app.config import settings

    if not settings.debug and "--force" not in sys.argv:
        logger.error(
            "Отказ: settings.debug=False (похоже на прод). Демо-сид "
            "создаёт аккаунты с общеизвестным паролем. Если вы уверены — "
            "повторите с флагом --force."
        )
        raise SystemExit(1)
    try:
        async with async_session_factory() as db:
            await seed(db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
