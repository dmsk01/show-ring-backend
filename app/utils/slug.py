"""
Генерация slug для блог-постов (этап 17).

slug — публичный URL и ключ lookup'а (`GET /posts/{slug}`). Голый kebab от
кириллицы дал бы пустую/мусорную строку, поэтому сначала транслитерируем
кириллицу в латиницу своей картой (без внешней зависимости), затем приводим
к kebab-case и обеспечиваем уникальность суффиксом -2, -3, … через колбэк
проверки занятости в БД.
"""

from __future__ import annotations

from typing import Awaitable, Callable

# Простая транслитерация кириллицы (ГОСТ-подобная, для читаемых URL).
# Знаки ъ/ь опускаются; ё→e. Этого достаточно для slug'ов, точность
# обратного преобразования не нужна.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(text: str) -> str:
    """Текст → kebab-case ascii. Кириллица транслитерируется, латиница/
    цифры остаются, всё остальное (пробелы, пунктуация) схлопывается в `-`."""
    out: list[str] = []
    for ch in text.lower().strip():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            # пробелы, пунктуация, нелатинские символы → разделитель
            out.append("-")
    slug = "".join(out)
    # Схлопываем повторяющиеся дефисы и срезаем по краям.
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


async def make_slug(
    title: str, exists: Callable[[str], Awaitable[bool]]
) -> str:
    """
    Уникальный slug из title. `exists(slug)` — async-колбэк, который
    возвращает True, если slug уже занят (проверка по БД). При коллизии
    добавляем числовой суффикс: foo, foo-2, foo-3, …

    UNIQUE-constraint на колонке slug всё равно страхует от гонок — этот
    цикл лишь делает URL читаемыми в обычном (неконкурентном) случае.
    """
    base = slugify(title) or "post"
    slug = base
    i = 2
    while await exists(slug):
        slug = f"{base}-{i}"
        i += 1
    return slug
