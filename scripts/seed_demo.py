r"""
Демо-сид для ручного тестирования UI: наполняет БД разнообразными
данными по всем основным разделам витрины (питомники, собаки с
родословной, помёты, выставки в РАЗНЫХ статусах, объявления, реклама).

В отличие от scripts.seed_test_show (узкий сценарий «одна завершённая
выставка для генерации документов»), этот скрипт даёт «широту»: списки,
фильтры, пагинацию есть на чём проверить.

Запуск:
    .\venv\Scripts\python.exe -m scripts.seed_demo

Идемпотентность:
- Справочники гарантируются через scripts.seed_references.
- Все сущности создаются через _get_or_create по натуральным ключам
  (email, kennel_prefix, rkf_number, имя выставки/объявления/кампании),
  поэтому повторный запуск не плодит дубли.
- Тяжёлый граф «выставка → ринги → записи → результаты» строится один
  раз: если выставка с тем же именем уже есть, он пропускается.

Чтобы не конфликтовать с seed_test_show (общие UNIQUE на rkf_number,
kennel_prefix, email), здесь используются отдельные пространства имён:
домены *-demo@dogshow.ru, приставки «… (демо)», номера RKF-DEMO-*.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import uuid
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import ClientError, EndpointConnectionError
from PIL import Image, ImageDraw
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, engine
# Регистрируем все модели в Base.metadata (ленивые FK).
from app.models import (  # noqa: F401
    ad, audit, classified, dog, file, kennel, litter,
    notification, outbox, reference, result, show, support, task,
)
from app.models.ad import AdBanner, AdCampaign, BannerPlacement, CampaignStatus
from app.models.classified import (
    Classified,
    ClassifiedCategory,
    ClassifiedPriceKind,
    ClassifiedStatus,
)
from app.models.dog import Dog, DogPhoto, SexEnum
from app.models.file import UploadedFile
from app.models.kennel import Kennel
from app.models.litter import Litter, LitterStatus
from app.models.reference import Breed, Grade, ShowClass, ShowRank
from app.models.result import ShowResult
from app.models.show import Show, ShowEntry, ShowJudge, ShowRing, ShowStatus
from app.models.user import RoleEnum, User, UserProfile, UserRole
from app.services import file_storage
from app.utils.security import hash_password
from scripts.seed_references import seed as seed_references

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed-demo")

DEMO_PASSWORD = "TestPass123!"
# Маркер «тяжёлый граф уже построен» — имя завершённой выставки.
SHOW_COMPLETED = "Кубок столицы — 2026 (демо)"
TODAY = date(2026, 6, 4)  # совпадает с currentDate проекта; стабильность сида


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


async def _user_with_profile(
    db, email, *, last, first, patr=None, country="Россия", role=None,
) -> User:
    # UserProfile хранит только ФИО + страну (город живёт у питомника /
    # объявления), поэтому city здесь не принимаем.
    user, _ = await _get_or_create(
        db, User, {"email": email},
        {
            "email": email,
            "hashed_password": hash_password(DEMO_PASSWORD),
            "is_active": True,
            "is_email_verified": True,
        },
    )
    await _get_or_create(
        db, UserProfile, {"user_id": user.id},
        {
            "user_id": user.id, "last_name": last, "first_name": first,
            "patronymic": patr, "country": country,
        },
    )
    if role is not None:
        await _get_or_create(
            db, UserRole, {"user_id": user.id, "role": role},
            {"user_id": user.id, "role": role, "granted_by": user.id},
        )
    return user


# ---------------------------------------------------------------------
# Демо-фото: генерация заглушек + привязка к собакам
# ---------------------------------------------------------------------

# Палитра фоновых цветов заглушечных фото (детерминированно по id собаки).
_PHOTO_PALETTE: list[tuple[int, int, int]] = [
    (76, 110, 159), (159, 76, 76), (76, 159, 99),
    (150, 120, 60), (110, 76, 159), (60, 130, 140),
]


def _make_photo_bytes(label: str, color: tuple[int, int, int]) -> bytes:
    """Заглушечный JPEG 640×480: цветной фон + подпись с кличкой."""
    img = Image.new("RGB", (640, 480), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 392, 640, 480), fill=(0, 0, 0))
    draw.text((24, 420), label[:48], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


async def _attach_photos(
    db: AsyncSession, dog: Dog, owner_id: uuid.UUID, count: int
) -> int:
    """
    Грузит до `count` заглушечных фото собаки в MinIO и связывает их
    через DogPhoto. Идемпотентно: догружает только недостающие позиции,
    поэтому повторный запуск сида не плодит дубли. Возвращает число
    реально добавленных фото.
    """
    existing = int((await db.execute(
        select(func.count()).select_from(DogPhoto)
        .where(DogPhoto.dog_id == dog.id)
    )).scalar_one())
    if existing >= count:
        return 0
    color = _PHOTO_PALETTE[dog.id.int % len(_PHOTO_PALETTE)]
    added = 0
    for pos in range(existing, count):
        label = dog.name if pos == 0 else f"{dog.name} · {pos + 1}"
        data = _make_photo_bytes(label, color)
        # upload_bytes не валидирует magic bytes — содержимое мы сами
        # сформировали Pillow'ом, оно гарантированно валидный JPEG.
        s3_key, size = await file_storage.upload_bytes(
            data, content_type="image/jpeg", extension="jpg", folder="dogs",
        )
        uploaded = UploadedFile(
            uploaded_by=owner_id,
            s3_key=s3_key,
            original_filename=f"{dog.name}-{pos + 1}.jpg",
            content_type="image/jpeg",
            size_bytes=size,
            # is_public=True — фото собак публичны, отдаются GET /files/{id}.
            is_public=True,
        )
        db.add(uploaded)
        await db.flush()
        db.add(DogPhoto(
            dog_id=dog.id, file_id=uploaded.id, position=pos,
            is_primary=(pos == 0),
        ))
        added += 1
    await db.flush()
    return added


# ---------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------


async def seed(db: AsyncSession) -> None:
    # 0. Справочники.
    await seed_references(db)

    # 1. Люди: организатор, два судьи, четыре заводчика, два покупателя.
    organizer = await _user_with_profile(
        db, "org-demo@dogshow.ru", last="Воронцова", first="Ирина",
        patr="Сергеевна", role=RoleEnum.organizer,
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
         "Аркадия", "Москва", "+7 (495) 100-10-10"),
        ("breeder2-demo@dogshow.ru", "Орлов", "Сергей", "Северная Звезда",
         "Северная Звезда", "Санкт-Петербург", "+7 (812) 200-20-20"),
        ("breeder3-demo@dogshow.ru", "Зайцева", "Марина", "Золотая Долина",
         "Золотая Долина", "Казань", "+7 (843) 300-30-30"),
        ("breeder4-demo@dogshow.ru", "Громов", "Павел", "Верный Друг",
         "Верный Друг", "Екатеринбург", "+7 (343) 400-40-40"),
    ]
    breeders: list[tuple[User, Kennel]] = []
    for i, (email, last, first, kname, prefix, city, phone) in enumerate(
        breeders_spec
    ):
        u = await _user_with_profile(
            db, email, last=last, first=first, role=RoleEnum.breeder,
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
        breeders.append((u, kennel))

    buyer1 = await _user_with_profile(
        db, "buyer1-demo@dogshow.ru", last="Соколов", first="Дмитрий",
        role=RoleEnum.buyer,
    )
    buyer2 = await _user_with_profile(
        db, "buyer2-demo@dogshow.ru", last="Морозова", first="Алина",
        role=RoleEnum.buyer,
    )

    # 2. Породы — берём первые 6 из справочника (стабильно по имени).
    breeds = (
        await db.execute(select(Breed).order_by(Breed.name).limit(6))
    ).scalars().all()
    if len(breeds) < 6:
        raise SystemExit("Мало пород в справочнике — запусти seed_references")

    # 3. Собаки. Для каждого питомника — пара производителей (отец/мать)
    #    своей породы и несколько потомков от этой пары. Так в карточках
    #    собак появляется родословная отец×мать.
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

    # dogs_by_breed[breed_idx] = list[Dog] потомков (для записей на выставку)
    dogs_by_breed: dict[int, list[Dog]] = {}
    # parents_by_breed[breed_idx] = (sire, dam)
    parents_by_breed: dict[int, tuple[Dog, Dog]] = {}
    # Собаки, которым прицепим демо-фото: (dog, owner_id, сколько фото).
    dog_specs: list[tuple[Dog, uuid.UUID, int]] = []

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
            parents_by_breed[breed_idx] = (sire, dam)
            dog_specs.append((sire, breeder_u.id, 1))
            dog_specs.append((dam, breeder_u.id, 1))

            # 3 потомка от этой пары.
            for k in range(3):
                sex = SexEnum.male if k % 2 == 0 else SexEnum.female
                names = pup_first
                name = (f"{names[(bi + k) % len(names)]} "
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
                    },
                )
                dogs_by_breed.setdefault(breed_idx, []).append(pup)
                # У щенков — галерея из 2 фото (главное + второе).
                dog_specs.append((pup, breeder_u.id, 2))

    # 3.5 Демо-фото: на каждую собаку генерируем заглушечный JPEG, грузим
    #     в MinIO и связываем через DogPhoto. Так avatar_file_id и
    #     photo_file_ids в ответе /dogs перестают быть пустыми, а GET
    #     /files/{id} реально отдаёт картинку из S3.
    n_photos = 0
    try:
        for dog_obj, owner_id, want in dog_specs:
            n_photos += await _attach_photos(db, dog_obj, owner_id, want)
    except (ClientError, EndpointConnectionError) as e:
        # Единственный «инфраструктурный» шаг сида — если MinIO не поднят,
        # даём внятную подсказку вместо падения по стеку boto3.
        raise SystemExit(
            "MinIO/S3 недоступен — демо-фото не загружены. Подними "
            "хранилище (docker compose up -d minio) и запусти сид заново. "
            f"Детали: {e}"
        )
    logger.info("Демо-фото загружено: %d", n_photos)

    # 4. Помёты: по одному на каждую (питомник × порода) пару. Ссылаемся на
    #    реальных производителей и проставляем litter_id всем щенкам этой
    #    пары — чтобы в карточке щенка litter_id не был null, а помёт
    #    показывал фактический состав. Все помёты с уже рождёнными щенками,
    #    поэтому статус planned не используем (он противоречил бы наличию
    #    привязанных щенков).
    litter_statuses = [
        LitterStatus.available, LitterStatus.born, LitterStatus.sold_out,
    ]
    li = 0
    for bi, (breeder_u, kennel) in enumerate(breeders):
        for breed_idx in (bi % len(breeds), (bi + 2) % len(breeds)):
            breed = breeds[breed_idx]
            # Производители (без родителей) и щенки (с родителями) именно
            # этого питомника и этой породы.
            producers = (await db.execute(
                select(Dog).where(
                    Dog.kennel_id == kennel.id,
                    Dog.breed_id == breed.id,
                    Dog.father_id.is_(None),
                )
            )).scalars().all()
            pups = (await db.execute(
                select(Dog).where(
                    Dog.kennel_id == kennel.id,
                    Dog.breed_id == breed.id,
                    Dog.father_id.is_not(None),
                )
            )).scalars().all()
            sire = next((d for d in producers if d.sex == SexEnum.male), None)
            dam = next((d for d in producers if d.sex == SexEnum.female), None)
            males = sum(1 for p in pups if p.sex == SexEnum.male)
            females = len(pups) - males
            status = litter_statuses[li % len(litter_statuses)]
            li += 1
            litter, _ = await _get_or_create(
                db, Litter,
                # Натуральный ключ помёта: питомник + порода.
                {"kennel_id": kennel.id, "breed_id": breed.id},
                {
                    "kennel_id": kennel.id,
                    "breed_id": breed.id,
                    "father_id": sire.id if sire else None,
                    "mother_id": dam.id if dam else None,
                    "born_at": TODAY - timedelta(days=40 + li * 10),
                    "puppies_count": len(pups),
                    "males_count": males,
                    "females_count": females,
                    "price_from": Decimal("40000.00"),
                    "price_to": Decimal("90000.00"),
                    "status": status,
                    "description": f"Помёт питомника «{kennel.name}», "
                                   f"порода {breed.name}. Родители с титулами, "
                                   "актированы, есть документы РКФ.",
                },
            )
            # Привязываем щенков этой пары к помёту (убираем null в litter_id).
            for p in pups:
                p.litter_id = litter.id

    # 5. Объявления — по всем категориям и видам цены.
    rank_default = (
        await db.execute(select(ShowRank).where(ShowRank.code == "cac-chf"))
    ).scalar_one()
    classifieds_spec = [
        (buyer1, ClassifiedCategory.puppy_sale, ClassifiedPriceKind.fixed,
         Decimal("65000.00"), breeds[0], "Москва",
         "Щенки на продажу — шоу-класс",
         "Продаются щенки от титулованных родителей. Привиты, "
         "клеймо, документы РКФ. Возможна доставка."),
        (breeders[0][0], ClassifiedCategory.adult_sale,
         ClassifiedPriceKind.negotiable, None, breeds[1], "Санкт-Петербург",
         "Взрослая собака в шоу-дом",
         "Перспективная сука, юный чемпион. Цена договорная для "
         "выставочного дома с амбициями."),
        (breeders[1][0], ClassifiedCategory.mating, ClassifiedPriceKind.fixed,
         Decimal("30000.00"), breeds[2], "Казань",
         "Вязка с интерчемпионом",
         "Предлагается кобель-производитель, интерчемпион, "
         "отличные тесты здоровья. Алименты или оплата."),
        (breeders[2][0], ClassifiedCategory.handler,
         ClassifiedPriceKind.negotiable, None, None, "Москва",
         "Услуги хендлера на выставках",
         "Опытный хендлер. Подготовка и показ в ринге, "
         "выставки любого ранга по РФ."),
        (breeders[3][0], ClassifiedCategory.grooming,
         ClassifiedPriceKind.fixed, Decimal("3500.00"), None, "Екатеринбург",
         "Груминг выставочных собак",
         "Профессиональный груминг к выставке: тримминг, "
         "стрижка, подготовка шерсти."),
        (buyer2, ClassifiedCategory.puppy_sale, ClassifiedPriceKind.free,
         None, breeds[3], "Казань",
         "Щенок в добрые руки",
         "Метис без документов ищет ответственных хозяев. "
         "Отдаётся бесплатно, привит."),
        (breeders[0][0], ClassifiedCategory.other,
         ClassifiedPriceKind.negotiable, None, None, "Москва",
         "Передержка и выгул",
         "Передержка собак на время отпуска владельцев. "
         "Домашние условия, опыт работы с шоу-собаками."),
    ]
    for author, category, price_kind, price, breed, city, title, descr in (
        classifieds_spec
    ):
        await _get_or_create(
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
                "status": ClassifiedStatus.active,
                "contact_phone": "+7 (900) 000-00-00",
                "contact_email": author.email,
            },
        )

    # 6. Реклама — кампания с баннерами в разных местах размещения.
    advertiser = breeders[0][0]
    campaign, _ = await _get_or_create(
        db, AdCampaign, {"name": "Корм PremiumDog — весна 2026 (демо)"},
        {
            "advertiser_id": advertiser.id,
            "name": "Корм PremiumDog — весна 2026 (демо)",
            "description": "Промо премиального корма для выставочных собак.",
            "budget": Decimal("50000.00"),
            "cost_per_impression": Decimal("0.50"),
            "date_start": TODAY - timedelta(days=10),
            "date_end": TODAY + timedelta(days=80),
            "status": CampaignStatus.active,
        },
    )
    banners_spec = [
        (BannerPlacement.top, "PremiumDog — скидка 20% на первый заказ"),
        (BannerPlacement.sidebar, "Корм для шоу-собак PremiumDog"),
        (BannerPlacement.inline, "Витамины для выставочной шерсти"),
    ]
    for placement, btitle in banners_spec:
        await _get_or_create(
            db, AdBanner,
            {"campaign_id": campaign.id, "title": btitle},
            {
                "campaign_id": campaign.id,
                "target_url": "https://example.com/premiumdog",
                "title": btitle,
                "placement": placement,
                "is_active": True,
            },
        )

    # 7. Выставки в разных статусах (для списков/фильтров витрины).
    #    Лёгкие (draft/registration_open/cancelled) — без графа записей.
    cls_open = (
        await db.execute(select(ShowClass).where(ShowClass.code == "open"))
    ).scalar_one()
    cls_junior = (
        await db.execute(select(ShowClass).where(ShowClass.code == "junior"))
    ).scalar_one()
    grade_exc = (
        await db.execute(select(Grade).where(Grade.code == "excellent"))
    ).scalar_one()

    light_shows = [
        ("Зимний кубок РКФ — 2026 (демо)", ShowStatus.completed,
         TODAY - timedelta(days=60), "Москва", "Крокус Экспо"),
        ("Весенняя выставка ЧФ (демо)", ShowStatus.registration_open,
         TODAY + timedelta(days=30), "Санкт-Петербург", "Экспофорум"),
        ("Летний CACIB (демо)", ShowStatus.registration_open,
         TODAY + timedelta(days=75), "Казань", "Казань Экспо"),
        ("Осенний национальный показ (демо)", ShowStatus.draft,
         TODAY + timedelta(days=120), "Екатеринбург", "Екатеринбург-ЭКСПО"),
        ("Монопородная (отменена) (демо)", ShowStatus.cancelled,
         TODAY + timedelta(days=20), "Москва", "Сокольники"),
    ]
    for name, status, dstart, city, venue in light_shows:
        await _get_or_create(
            db, Show, {"name": name},
            {
                "organizer_id": organizer.id,
                "rank_id": rank_default.id,
                "name": name,
                "description": "Сертификатная выставка. Запись онлайн, "
                               "ринги по группам FCI, эксперты РКФ.",
                "date_start": dstart,
                "city": city,
                "country": "Россия",
                "venue": venue,
                "entry_fee": Decimal("2500.00"),
                "registration_deadline": dstart - timedelta(days=7),
                "status": status,
            },
        )

    # 8. Тяжёлый граф: одна завершённая выставка с рингами, записями и
    #    результатами. Строим один раз (маркер — имя выставки).
    existing = (
        await db.execute(select(Show).where(Show.name == SHOW_COMPLETED))
    ).scalar_one_or_none()
    if existing is None:
        show = Show(
            organizer_id=organizer.id, rank_id=rank_default.id,
            name=SHOW_COMPLETED,
            description="Завершённая выставка с опубликованными результатами.",
            date_start=TODAY - timedelta(days=14), city="Москва",
            country="Россия", venue="ВДНХ, павильон 75",
            entry_fee=Decimal("3000.00"), status=ShowStatus.completed,
        )
        db.add(show)
        await db.flush()

        # Ринги по трём первым породам + назначения судей.
        judges_cycle = [judge1, judge2, judge1]
        ring_breeds = breeds[:3]
        for i, breed in enumerate(ring_breeds):
            jdg = judges_cycle[i]
            db.add(ShowRing(
                show_id=show.id, ring_number=i + 1, breed_id=breed.id,
                judge_id=jdg.id, ring_date=show.date_start,
                time_start=time(10, 0), location=f"Ринг №{i + 1}",
            ))
            db.add(ShowJudge(
                show_id=show.id, judge_id=jdg.id, breed_id=breed.id
            ))

        # Записи: берём потомков первых трёх пород.
        catalog_no = 0
        for bi, breed in enumerate(ring_breeds):
            for di, d in enumerate(dogs_by_breed.get(bi, [])):
                catalog_no += 1
                # Класс по возрасту: молодые → юниоры, старше → открытый.
                cls = cls_junior if d.date_of_birth and \
                    d.date_of_birth >= date(2024, 1, 1) else cls_open
                entry = ShowEntry(
                    show_id=show.id, dog_id=d.id, show_class_id=cls.id,
                    catalog_number=catalog_no, registered_by=organizer.id,
                )
                db.add(entry)
                await db.flush()
                # Результат: лучшему в породе — титулы.
                is_winner = di == 0
                titles = (
                    [{"code": "CW", "name": "CW"},
                     {"code": "CAC", "name": "CAC"},
                     {"code": "BOB", "name": "BOB"}]
                    if is_winner else [{"code": "CW", "name": "CW"}]
                )
                db.add(ShowResult(
                    show_entry_id=entry.id, judge_id=judges_cycle[bi].id,
                    grade_id=grade_exc.id, placement=di + 1,
                    is_class_winner=is_winner, is_best_of_breed=is_winner,
                    titles_cache=titles,
                ))

    await db.commit()
    await _print_summary(db)


async def _print_summary(db: AsyncSession) -> None:
    n_shows = len((await db.execute(select(Show.id))).scalars().all())
    n_dogs = len((await db.execute(select(Dog.id))).scalars().all())
    n_kennels = len((await db.execute(select(Kennel.id))).scalars().all())
    n_litters = len((await db.execute(select(Litter.id))).scalars().all())
    n_class = len((await db.execute(select(Classified.id))).scalars().all())
    n_photos = len((await db.execute(select(DogPhoto.id))).scalars().all())
    print("\n" + "=" * 60)
    print("ДЕМО-СИД ГОТОВ")
    print(f"  выставок:   {n_shows}")
    print(f"  собак:      {n_dogs}")
    print(f"  фото собак: {n_photos}")
    print(f"  питомников: {n_kennels}")
    print(f"  помётов:    {n_litters}")
    print(f"  объявлений: {n_class}")
    print(f"  логины (пароль у всех {DEMO_PASSWORD}):")
    print("    org-demo@dogshow.ru        — организатор")
    print("    breeder1-demo@dogshow.ru   — заводчик")
    print("    buyer1-demo@dogshow.ru     — покупатель")
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
