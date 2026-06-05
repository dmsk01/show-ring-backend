"""
Allowlist-санитизация HTML-контента блог-постов (этап 17).

content приходит из WYSIWYG-редактора фронта — это классический XSS-вектор,
поэтому на записи (POST/PUT) прогоняем его через bleach с белым списком:
разрешаем форматирование/заголовки/списки/ссылки/картинки, вырезаем
<script>, обработчики on* и протокол javascript:.

Глобальный SanitizationMiddleware это поле НЕ трогает (content в passthrough,
см. app/middleware/sanitization.py) — иначе он вырезал бы весь HTML целиком
(tags=[], strip=True) ещё до хендлера, и блог отдавал бы пустой текст.
"""

from __future__ import annotations

import re

import bleach

# bleach.clean(strip=True) убирает САМ тег <script>/<style>, но оставляет его
# текстовое содержимое как inert-текст ("alert(1)"). Исполнения уже нет (тега
# нет — это безопасно), но текст-мусор остаётся. Вырезаем содержимое целиком
# ДО bleach: узкий случай (script/style — rawtext-элементы, внутри них не
# бывает легитимной разметки поста). Если злоумышленник пришлёт незакрытый
# <script> — regex его не тронет, но bleach всё равно срежет сам тег, так что
# XSS-вектор закрыт в любом случае.
_RAWTEXT_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)

# Разрешённые теги: форматирование текста, заголовки h1..h4, списки, цитаты,
# код, ссылки и картинки. Всё, чего тут нет, bleach вырежет (strip=True).
ALLOWED_TAGS = [
    "p", "h1", "h2", "h3", "h4", "strong", "em", "u", "a", "ul", "ol", "li",
    "blockquote", "code", "pre", "img", "br", "span",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt"],
}
# Абсолютные ссылки — только http/https. Относительные URL (например
# /files/{id} для картинок-обложек) bleach пропускает: у них нет схемы,
# и проверка протокола к ним не применяется. javascript:/data: → href/src
# вырезаются.
ALLOWED_PROTOCOLS = ["http", "https"]


def sanitize_post_html(html: str) -> str:
    """Очищает пользовательский HTML по allowlist. Пустой вход → пустая строка."""
    if not html:
        return ""
    html = _RAWTEXT_RE.sub("", html)
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
