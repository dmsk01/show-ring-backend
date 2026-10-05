"""Ограничение размера страницы для анонимных запросов (план защиты 2026-10-05)."""

from __future__ import annotations

from app.models.user import User

# Витрины фронта показывают до 48 карточек — 50 хватает с запасом.
ANON_MAX_PER_PAGE = 50
# Результаты и каталог выставки фронт грузит по 200 записей.
ANON_MAX_PER_PAGE_LARGE = 200


def cap_per_page(
    per_page: int, viewer: User | None, limit: int = ANON_MAX_PER_PAGE
) -> int:
    """
    Аноним получает не больше `limit` записей за запрос, вошедший — сколько
    просил (в пределах Query le=). Публичные списки до 200–1000 записей за
    раз при 20 запросах/с — тысячи записей в секунду с одного IP: удобно для
    выкачивания базы и для нагрузки на БД. Ответ возвращает фактический
    per_page, так что клиент видит, что страница урезана.
    """
    if viewer is not None:
        return per_page
    return min(per_page, limit)
