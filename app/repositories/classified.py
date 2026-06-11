"""
Репозиторий объявлений (этап 5).

Здесь сосредоточена ключевая SQL-логика этапа:
1. Динамические фильтры — собираются на SQLAlchemy Core, где условия
   добавляются только если параметр задан.
2. Полнотекстовый поиск — через сгенерированный столбец search_vector
   и функцию plainto_tsquery. ts_rank() даёт релевантность для
   сортировки.
3. Атомарный инкремент views_count — UPDATE … SET v = v + 1, чтобы
   избежать race condition при двух одновременных GET /{id}.
4. LEFT JOIN с classified_images — каждое объявление возвращается
   вместе со списком фото (через selectinload — отдельный запрос за
   изображениями, а не "распухание" основного через JOIN).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Sequence

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.classified import (
    AnimalAvailability,
    Classified,
    ClassifiedCategory,
    ClassifiedImage,
    ClassifiedStatus,
)
from app.models.dog import SexEnum


# Белый список сортировки (этап 18).
_CLASSIFIED_SORT = {
    "created_at": Classified.created_at,
    "price": Classified.price,
    "views_count": Classified.views_count,
}


def _classified_filter_stmt(
    category: ClassifiedCategory | None,
    breed_id: uuid.UUID | None,
    sex: SexEnum | None,
    city: str | None,
    status: ClassifiedStatus | None,
    availability: AnimalAvailability | None,
    price_from: Decimal | None,
    price_to: Decimal | None,
    author_id: uuid.UUID | None,
):
    """
    Собирает базовый SELECT с динамическими WHERE-условиями.

    Логика "условие добавляется только если параметр задан" — это самый
    частый паттерн фильтрации в продакшен-API. Не строим строки SQL
    через format() — SQLAlchemy сам биндит параметры безопасно.
    """
    stmt = select(Classified)
    if category is not None:
        stmt = stmt.where(Classified.category == category)
    if breed_id is not None:
        stmt = stmt.where(Classified.breed_id == breed_id)
    if sex is not None:
        # Точечный фильтр: ?sex=male отдаёт только male. Объявления с
        # sex IS NULL (услуги, смешанные помёты) под него не попадают —
        # это согласованный с фронтом контракт.
        stmt = stmt.where(Classified.sex == sex)
    if city:
        # ILIKE — для удобства: пользователь ищет "Москва" / "москв".
        # В продакшене города стоит нормализовать через справочник,
        # но на этапе 5 это не цель.
        stmt = stmt.where(Classified.city.ilike(f"%{city}%"))
    if status is not None:
        stmt = stmt.where(Classified.status == status)
    if availability is not None:
        # Точечный фильтр доступности: ?availability=available отдаёт
        # только свободных. Объявление остаётся active даже когда животное
        # sold/reserved — этот фильтр позволяет покупателю отсеять занятых.
        stmt = stmt.where(Classified.availability == availability)
    if price_from is not None:
        stmt = stmt.where(Classified.price >= price_from)
    if price_to is not None:
        stmt = stmt.where(Classified.price <= price_to)
    if author_id is not None:
        stmt = stmt.where(Classified.author_id == author_id)
    return stmt


async def get_classified(
    db: AsyncSession, id_: uuid.UUID, *, with_images: bool = True
) -> Classified | None:
    stmt = select(Classified).where(Classified.id == id_)
    if with_images:
        # selectinload — лёгкий способ загрузить связанные images одним
        # дополнительным запросом, без N+1. Альтернатива joinedload
        # дала бы декартово произведение и больший трафик.
        stmt = stmt.options(selectinload(Classified.images))
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_classifieds(
    db: AsyncSession,
    *,
    category: ClassifiedCategory | None = None,
    breed_id: uuid.UUID | None = None,
    sex: SexEnum | None = None,
    city: str | None = None,
    status: ClassifiedStatus | None = ClassifiedStatus.active,
    availability: AnimalAvailability | None = None,
    price_from: Decimal | None = None,
    price_to: Decimal | None = None,
    author_id: uuid.UUID | None = None,
    sort_by: str = "created_at",
    order: str = "desc",
    page: int = 1,
    per_page: int = 50,
) -> Sequence[Classified]:
    # Свежие сверху по умолчанию. По created_at, не updated_at — последняя
    # правка автора не должна "выталкивать" объявление в топ списка.
    col = _CLASSIFIED_SORT.get(sort_by, Classified.created_at)
    stmt = (
        _classified_filter_stmt(
            category, breed_id, sex, city, status, availability,
            price_from, price_to, author_id,
        )
        .order_by(col.asc() if order == "asc" else col.desc())
        .options(selectinload(Classified.images))
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count_classifieds(
    db: AsyncSession,
    *,
    category: ClassifiedCategory | None = None,
    breed_id: uuid.UUID | None = None,
    sex: SexEnum | None = None,
    city: str | None = None,
    status: ClassifiedStatus | None = ClassifiedStatus.active,
    availability: AnimalAvailability | None = None,
    price_from: Decimal | None = None,
    price_to: Decimal | None = None,
    author_id: uuid.UUID | None = None,
) -> int:
    base = _classified_filter_stmt(
        category, breed_id, sex, city, status, availability,
        price_from, price_to, author_id,
    ).subquery()
    return int(
        (await db.execute(select(func.count()).select_from(base))).scalar_one()
    )


async def create_classified(db: AsyncSession, **fields) -> Classified:
    obj = Classified(**fields)
    db.add(obj)
    await db.flush()
    return obj


async def add_image(
    db: AsyncSession,
    classified_id: uuid.UUID,
    file_id: uuid.UUID,
    position: int = 0,
    is_primary: bool = False,
) -> ClassifiedImage:
    img = ClassifiedImage(
        classified_id=classified_id,
        file_id=file_id,
        position=position,
        is_primary=is_primary,
    )
    db.add(img)
    await db.flush()
    return img


async def increment_views(db: AsyncSession, id_: uuid.UUID) -> None:
    """
    Атомарный инкремент через UPDATE — не считываем views_count в Python,
    не плюсуем, не пишем обратно (это была бы race condition при
    одновременных запросах). Один SQL-вызов — одно изменение.
    """
    await db.execute(
        update(Classified)
        .where(Classified.id == id_)
        .values(views_count=Classified.views_count + 1)
    )
    await db.commit()


# ---------------------------------------------------------------------
# Full-text search
# ---------------------------------------------------------------------


# Используем text() с биндингом параметра :query. Никакой f-строки —
# защита от SQL-инъекции. plainto_tsquery превращает пользовательский
# текст ("немецкая овчарка щенок") в безопасный tsquery без необходимости
# учить пользователя синтаксису "&|!".
FTS_SEARCH_SQL = text(
    """
    SELECT
        c.id,
        ts_rank(c.search_vector, plainto_tsquery('russian', :query)) AS rank
    FROM classifieds c
    WHERE c.search_vector @@ plainto_tsquery('russian', :query)
      AND c.status = 'active'
    ORDER BY rank DESC, c.created_at DESC
    LIMIT :limit
    OFFSET :offset
    """
)


async def search_classifieds(
    db: AsyncSession,
    query: str,
    page: int = 1,
    per_page: int = 50,
) -> list[Classified]:
    """
    Полнотекстовый поиск.

    Шаги:
    1. Сырой SQL возвращает id найденных объявлений + rank релевантности.
       Это быстро (используется GIN-индекс), но возвращает только id.
    2. Догружаем сами объекты с images через selectinload, сохраняя
       порядок, заданный поисковым запросом.
    """
    rows = await db.execute(
        FTS_SEARCH_SQL,
        {
            "query": query,
            "limit": per_page,
            "offset": (page - 1) * per_page,
        },
    )
    id_to_rank: dict[uuid.UUID, float] = {
        row.id: row.rank for row in rows.fetchall()
    }
    if not id_to_rank:
        return []

    stmt = (
        select(Classified)
        .where(Classified.id.in_(id_to_rank.keys()))
        .options(selectinload(Classified.images))
    )
    objs = list((await db.execute(stmt)).scalars().all())
    # Сортируем результат в том же порядке, который вернул FTS-запрос
    # (по убыванию rank). Иначе SELECT … WHERE id IN (...) даст
    # произвольный порядок.
    objs.sort(key=lambda c: id_to_rank.get(c.id, 0), reverse=True)
    return objs


async def count_search_results(db: AsyncSession, query: str) -> int:
    """
    Считаем общее число результатов отдельным запросом. Не самое
    эффективное (PG считает совпадения), но без CURSOR_BASED API
    это стандартный приём для пагинации.
    """
    result = await db.execute(
        text(
            """
            SELECT COUNT(*) AS cnt
            FROM classifieds
            WHERE search_vector @@ plainto_tsquery('russian', :query)
              AND status = 'active'
            """
        ),
        {"query": query},
    )
    return int(result.scalar_one())
