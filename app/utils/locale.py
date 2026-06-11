"""
Локализация справочников по Accept-Language (спека 2026-06-11).

Поддерживаем два языка: ru (канонический, лежит в name/description)
и en (перевод в name_en/description_en). Язык не определён или не
поддерживается → ru. Контракт API не меняется: сервер резолвит локаль
и отдаёт нужный текст в существующих полях name/description.

Парсер заголовка свой и минимальный (а не сторонняя библиотека):
нам нужен только выбор из двух языков по q-весам — полноценный
RFC 9110 language matching здесь избыточен.
"""

from __future__ import annotations

from typing import Literal, TypeVar

from fastapi import Header
from pydantic import BaseModel

Locale = Literal["ru", "en"]

SUPPORTED_LOCALES: tuple[Locale, ...] = ("ru", "en")
DEFAULT_LOCALE: Locale = "ru"


def resolve_locale(accept_language: str | None) -> Locale:
    """
    Выбирает поддерживаемую локаль из Accept-Language.

    Правила:
    - региональные подтеги сводятся к основному языку (en-US → en);
    - побеждает поддерживаемый язык с максимальным q (дефолт q=1);
    - при равных q — первый по порядку в заголовке;
    - q=0 ("неприемлем") и битые q-значения исключают токен;
    - ничего не подошло → DEFAULT_LOCALE.
    """
    if not accept_language:
        return DEFAULT_LOCALE

    best_lang: Locale | None = None
    best_q = 0.0
    for token in accept_language.split(","):
        lang_part, _, q_part = token.strip().partition(";")
        primary = lang_part.strip().lower().split("-")[0]
        if primary not in SUPPORTED_LOCALES:
            continue
        q = 1.0
        q_part = q_part.strip()
        if q_part.startswith("q="):
            try:
                q = float(q_part[2:])
            except ValueError:
                continue
        # Строгое сравнение: при равных q остаётся первый встреченный язык.
        if q > best_q:
            best_q = q
            best_lang = primary  # type: ignore[assignment] — primary ∈ SUPPORTED_LOCALES

    return best_lang or DEFAULT_LOCALE


def get_locale(accept_language: str | None = Header(None)) -> Locale:
    """FastAPI-зависимость: локаль ответа из заголовка Accept-Language."""
    return resolve_locale(accept_language)


ModelT = TypeVar("ModelT", bound=BaseModel)


def localize(resp: ModelT, locale: Locale) -> ModelT:
    """
    Подменяет name/description на английские поля для locale="en".

    Работает с Pydantic-копией (model_validate от ORM), а НЕ с ORM-объектом:
    мутация ORM-инстанса пометила бы его dirty и могла бы утечь в БД при
    autoflush. Пустой перевод → фолбэк на русский (поле не трогаем).
    """
    if locale == "en":
        name_en = getattr(resp, "name_en", None)
        if name_en:
            resp.name = name_en  # type: ignore[attr-defined]
        description_en = getattr(resp, "description_en", None)
        if description_en:
            resp.description = description_en  # type: ignore[attr-defined]
    return resp
