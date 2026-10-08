"""
Allowlist-санитизация HTML-контента блог-постов (этап 17).

content приходит из WYSIWYG-редактора фронта — это классический XSS-вектор,
поэтому на записи (POST/PUT) прогоняем его через nh3 (Rust-библиотека
ammonia; до ревью 2026-10-06, BE-29 — bleach, который больше не
развивается) с белым списком: разрешаем форматирование/заголовки/списки/
ссылки/картинки, вырезаем <script>, обработчики on* и протокол javascript:.

Глобальный SanitizationMiddleware это поле НЕ трогает (content в passthrough,
см. app/middleware/sanitization.py) — иначе он вырезал бы весь HTML целиком
ещё до хендлера, и блог отдавал бы пустой текст.
"""

from __future__ import annotations

import nh3

# Белый список под WYSIWYG-редактор: форматирование текста, заголовки, списки,
# код, ссылки и картинки. Всё, чего тут нет, nh3 вырежет. Содержимое
# <script>/<style> удаляется целиком (clean_content_tags по умолчанию).
ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "strong", "em", "u", "a", "ul", "ol", "li",
    "blockquote", "code", "pre", "img", "br", "span",
}
# rel не в списке: nh3 сам проставляет ссылкам rel="noopener noreferrer"
# (защита от tabnabbing при target="_blank").
ALLOWED_ATTRS = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt"},
}
# Только http/https. Относительные URL (/files/{id} для картинок-обложек)
# пропускаются: у них нет схемы. javascript:/data: → href/src вырезаются.
ALLOWED_PROTOCOLS = {"http", "https"}


def sanitize_post_html(html: str) -> str:
    """Очищает пользовательский HTML по allowlist. Пустой вход → пустая строка."""
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes=ALLOWED_PROTOCOLS,
        link_rel="noopener noreferrer",
    )
