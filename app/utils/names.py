# app/utils/names.py
"""
Чистые хелперы для человекочитаемых имён в официальных документах.

Принимают объект с .email и (опционально) .profile с полями last_name/
first_name/patronymic/country. Не делают запросов в БД — вызывающий код
обязан загрузить profile заранее (через selectinload или awaitable_attrs).
"""

from __future__ import annotations

from typing import Any


def full_name(user: Any | None) -> str:
    """«Фамилия Имя Отчество», пустые части опускаются. Если профиль пуст —
    fallback на email. None → пустая строка."""
    if user is None:
        return ""
    profile = getattr(user, "profile", None)
    if profile is not None:
        parts = [
            getattr(profile, "last_name", None),
            getattr(profile, "first_name", None),
            getattr(profile, "patronymic", None),
        ]
        joined = " ".join(p.strip() for p in parts if p and p.strip())
        if joined:
            return joined
    return getattr(user, "email", "") or ""


def judge_display(user: Any | None) -> str:
    """«Фамилия Имя Отчество (Страна)». Без страны — только ФИО."""
    name = full_name(user)
    profile = getattr(user, "profile", None)
    country = getattr(profile, "country", None) if profile is not None else None
    if country and country.strip():
        return f"{name} ({country.strip()})"
    return name
