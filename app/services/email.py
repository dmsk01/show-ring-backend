"""
Email сервис: шаблоны (Jinja2) + SMTP-отправка (aiosmtplib) (этап 9).

Шаблоны email лежат в `app/templates/email/<event_type>.html.j2`.
Каждый шаблон содержит три Jinja-блока:
- {% block subject %}…{% endblock %} — заголовок письма
- {% block html %}…{% endblock %}    — HTML-тело
- {% block text %}…{% endblock %}    — plain-text fallback

Один файл на шаблон проще, чем три отдельных subject.txt/body.html/body.txt
для каждого события: меньше файлов, рядом — всё, что нужно.

SMTP-настройки берутся из app.config.settings (smtp_host, smtp_port,
smtp_use_tls и т.д.). По умолчанию указывают на MailPit — локальный
SMTP без auth и без TLS, удобный для разработки.
"""

from __future__ import annotations

import logging
import re
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from markupsafe import escape as _html_escape

from app.config import settings

logger = logging.getLogger(__name__)


# Директория шаблонов. Абсолютный путь относительно файла, чтобы
# работало независимо от cwd запуска (FastAPI / Worker).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"


# Jinja Environment кэширует шаблоны — переиспользуем один на процесс.
# autoescape для html — защита от XSS, если в payload событий придёт
# пользовательский контент.
#
# ИСПРАВЛЕНО (bug_011 ultrareview): без явного enabled_extensions
# select_autoescape сверяет с дефолтом ('html','htm','xml'). Все наши
# шаблоны named *.html.j2 — trailing extension `.j2` НЕ совпадает с
# дефолтным списком, поэтому autoescape был молчаливо ВЫКЛЮЧЕН для
# каждого письма, и organizer/breeder-контролируемые поля
# (show_name, kennel_name, dog_name) шли сырыми в HTML подписчикам.
# Включаем расширение 'j2' явно. Все шаблоны в templates/email/ —
# HTML, escape unconditional безопасен.
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(
        enabled_extensions=("html", "htm", "xml", "j2"),
        default_for_string=False,
        default=False,
    ),
    enable_async=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


# ---------------------------------------------------------------------
# Рендер шаблона
# ---------------------------------------------------------------------


def render_email(
    template_name: str, context: dict
) -> tuple[str, str, str]:
    """
    Рендерит шаблон <template_name>.html.j2 в три части: (subject,
    html_body, text_body).

    Если шаблон не найден — отдаём общий fallback с типом события.
    Это лучше, чем падать с 500: пользователь получит "что-то", и
    разработчик увидит факт по логам.
    """
    try:
        template = _env.get_template(f"{template_name}.html.j2")
    except TemplateNotFound:
        logger.warning("Email template not found: %s", template_name)
        # ИСПРАВЛЕНО (review 2026-05-28): template_name приходит из
        # event.event_type. Сейчас он внутренний enum, но если когда-нибудь
        # источник событий расширится на внешний publisher, event_type
        # может стать управляемым. Подстановка через f-string в HTML без
        # экранирования давала бы XSS в письме. markupsafe.escape — тот же
        # механизм, что использует Jinja для autoescape. text-fallback
        # оставляем как есть (это plain/text, без HTML-парсинга у клиента).
        return (
            f"ShowTail: {template_name}",
            f"<p>Событие: {_html_escape(template_name)}</p>",
            f"Событие: {template_name}",
        )

    # Jinja-блоки рендерятся через get_block_renderers, но проще
    # вызвать render с context и использовать "макро через include" —
    # тут проще всё-же три отдельных функции через .module.
    # Универсальный путь: рендерим шаблон целиком, а блоки берём как
    # атрибуты у скомпилированной module.
    module = template.make_module(context)

    def _render_block(block_name: str) -> str:
        # get_block_render через template.blocks возвращает функцию,
        # которой нужен Context. Гладкое чтение блоков через make_module
        # не поддерживается напрямую — используем низкоуровневое API.
        block_fn = template.blocks.get(block_name)
        if block_fn is None:
            return ""
        ctx = template.new_context(context)
        return "".join(block_fn(ctx))

    subject = _render_block("subject").strip()
    html_body = _render_block("html").strip()
    text_body = _render_block("text").strip()

    # Fallback subject — на случай если шаблон не задал блок.
    if not subject:
        subject = f"ShowTail: {template_name}"
    # Если text-fallback не задан, генерим из html через rude strip.
    # Это не идеальный конвертер, но лучше, чем пустой text/plain.
    if not text_body and html_body:
        text_body = _strip_html(html_body)

    # module не используется напрямую, но make_module мог обновить
    # переменные globals — оставляем переменную чтобы линтер не ругался.
    _ = module
    return subject, html_body, text_body


def _strip_html(html: str) -> str:
    """
    Грубая конвертация HTML → plain text. Не нужна полная реализация:
    text body используется только клиентами, которые не умеют HTML
    (древние, server-side). Простого strip достаточно.
    """
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # Сжимаем тройные переводы в двойные.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------
# SMTP-отправка
# ---------------------------------------------------------------------


async def send_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
) -> None:
    """
    Отправляет письмо через aiosmtplib. Используется воркером — в API
    напрямую не дёргаем (HTTP-handler не должен ждать ответ SMTP-сервера).

    Формат — EmailMessage с двумя альтернативами (text/plain + text/html).
    Это правильный multipart: умные клиенты покажут HTML, простые
    fallback'нутся на text.
    """
    msg = EmailMessage()
    msg["From"] = (
        f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    )
    msg["To"] = to_email
    msg["Subject"] = subject
    # Сначала добавляем text — он становится "первичной альтернативой".
    msg.set_content(text_body or _strip_html(html_body))
    # add_alternative делает multipart/alternative с HTML-вариантом.
    msg.add_alternative(html_body, subtype="html")

    # use_tls — для MailPit False. В prod (Sendgrid/SES) — True.
    # username/password опциональны: MailPit принимает без auth.
    await aiosmtplib.send(
        msg,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        use_tls=settings.smtp_use_tls,
        # start_tls — отдельный механизм (STARTTLS), нужен для портов
        # 587. На MailPit не нужно.
        start_tls=False,
    )
    logger.info("Email sent to %s: %s", to_email, subject)
