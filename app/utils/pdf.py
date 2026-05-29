"""
PDF-генерация для каталогов и дипломов (этап 8).

Используется ReportLab в режиме "программного" рендера (платформа,
а не HTML→PDF как у WeasyPrint).

Почему ReportLab, а не WeasyPrint:
- Pure-Python, без системных зависимостей (Pango/Cairo).
- На Windows работает без танцев с DLL.
- Минус — вёрстка ручная (нет CSS), но для каталогов выставок этого
  достаточно.

Кириллица в PDF. Стандартные шрифты ReportLab (Helvetica, Times) не
поддерживают кириллицу. Нужен TTF Unicode-шрифт. Мы:
1. Пытаемся зарегистрировать первый найденный системный TTF
   (DejaVuSans на Linux, Arial на Windows, …).
2. Если ничего не нашли — log.warning и продолжаем без регистрации
   (PDF будет с "квадратами" вместо русских букв). Лучше отдать кривой
   PDF, чем падать.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.document import CatalogData, DiplomaData

logger = logging.getLogger(__name__)


# ИСПРАВЛЕНО (review 2026-05-28): ReportLab Paragraph использует
# XML-подобную разметку (`<b>`, `<i>`, `<font>` …). Пользовательские
# значения — клички собак, имена владельцев, названия питомников —
# попадали в Paragraph через f-string без эскейпа. Кличка `<i>Bobby</i>`
# не только не отображалась бы корректно, но при `<` без закрытия
# Paragraph падал бы на парсинге XML, ронив весь каталог (PDF на 1000
# собак — крэш из-за одной "битой" записи).
# _esc — единая обёртка: None → пустая строка, остальное прогоняется
# через xml.sax.saxutils.escape (тот же набор &/<,>/"/').
def _esc(value: object | None) -> str:
    """Безопасное преобразование произвольного значения в Paragraph-текст."""
    if value is None:
        return ""
    return _xml_escape(str(value), {'"': "&quot;", "'": "&apos;"})


# ---------------------------------------------------------------------
# Регистрация шрифта (кириллица)
# ---------------------------------------------------------------------


_FONT_NAME = "AppFont"
_FONT_BOLD = "AppFont-Bold"
_font_registered = False

# Кандидаты на роль системного TTF с поддержкой кириллицы. Порядок —
# приоритет: сначала легковесные, потом классические. Меняем порядок,
# если на конкретной платформе нужны другие.
_FONT_CANDIDATES: list[tuple[str, str]] = [
    # Linux (Debian/Ubuntu)
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    # Linux (другие дистрибутивы)
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    # Windows
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    # macOS
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]


def _register_font_once() -> str:
    """
    Регистрирует первый найденный шрифт, возвращает имя зарегистрированного.
    Если ничего не нашлось — возвращает 'Helvetica' (стандартный, не
    русский). Идемпотентна: повторный вызов ничего не делает.
    """
    global _font_registered
    if _font_registered:
        return _FONT_NAME
    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).exists():
            try:
                pdfmetrics.registerFont(TTFont(_FONT_NAME, regular))
                # Жирный — опционально: если файла нет, не страшно.
                if Path(bold).exists():
                    pdfmetrics.registerFont(TTFont(_FONT_BOLD, bold))
                _font_registered = True
                logger.info("PDF font registered: %s", regular)
                return _FONT_NAME
            except Exception as e:  # pragma: no cover — реальные ошибки I/O
                logger.warning("Cannot register font %s: %s", regular, e)
                continue
    logger.warning(
        "No Unicode TTF found — кириллица в PDF будет некорректной"
    )
    return "Helvetica"


def _bold_name() -> str:
    """Возвращает имя жирного шрифта или fallback на основной."""
    return _FONT_BOLD if _FONT_BOLD in pdfmetrics.getRegisteredFontNames() else _FONT_NAME


# ---------------------------------------------------------------------
# Стили
# ---------------------------------------------------------------------


def _make_styles() -> dict[str, ParagraphStyle]:
    """
    Кэширует стили на каждый рендер: ParagraphStyle мутабельный объект,
    и переиспользование между документами может давать побочные эффекты.
    """
    font = _register_font_once()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=base["Title"],
            fontName=_bold_name(),
            fontSize=18,
            leading=22,
        ),
        "h1": ParagraphStyle(
            "h1",
            parent=base["Heading1"],
            fontName=_bold_name(),
            fontSize=14,
            leading=18,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontName=_bold_name(),
            fontSize=12,
            leading=15,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["BodyText"],
            fontName=font,
            fontSize=10,
            leading=13,
        ),
        "small": ParagraphStyle(
            "small",
            parent=base["BodyText"],
            fontName=font,
            fontSize=8,
            leading=10,
            textColor=colors.grey,
        ),
    }


# ---------------------------------------------------------------------
# Каталог
# ---------------------------------------------------------------------


def render_catalog(data: CatalogData) -> bytes:
    """
    Рендерит каталог выставки в bytes. Возвращает содержимое PDF —
    дальше воркер сам кладёт его в MinIO.

    BytesIO здесь, а не файл на диске, потому что воркер сразу аплодит
    в S3, и файл на диске никому не нужен.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title=data.show_name,
    )
    styles = _make_styles()
    story: list = []

    # Шапка. Все user-supplied поля прогоняем через _esc — см. модуль выше.
    story.append(Paragraph(_esc(data.show_name), styles["title"]))
    period = (
        f"{data.date_start.strftime('%d.%m.%Y')}"
        + (f" — {data.date_end.strftime('%d.%m.%Y')}" if data.date_end else "")
    )
    story.append(
        Paragraph(
            f"Ранг: {_esc(data.show_rank)} &nbsp; • &nbsp; Даты: {period}"
            + (f" &nbsp; • &nbsp; Город: {_esc(data.city)}" if data.city else "")
            + (f" &nbsp; • &nbsp; Место: {_esc(data.venue)}" if data.venue else ""),
            styles["body"],
        )
    )
    story.append(
        Paragraph(f"Всего записей: {data.total_entries}", styles["small"])
    )
    story.append(Spacer(1, 0.4 * cm))

    # Судьи.
    if data.judges:
        story.append(Paragraph("Судьи", styles["h2"]))
        for j in data.judges:
            story.append(
                Paragraph(
                    f"• {_esc(j.name)} — {_esc(j.breeds_or_groups)}",
                    styles["body"],
                )
            )
        story.append(Spacer(1, 0.4 * cm))

    # Породы и классы.
    for section in data.breed_sections:
        group_label = (
            f"FCI группа {section.group_number}, " if section.group_number else ""
        )
        fci_label = f" (FCI №{_esc(section.fci_number)})" if section.fci_number else ""
        story.append(
            Paragraph(
                f"{group_label}{_esc(section.breed_name)}{fci_label}",
                styles["h1"],
            )
        )
        if section.judge_name:
            story.append(
                Paragraph(
                    f"Судья: {_esc(section.judge_name)}",
                    styles["body"],
                )
            )
        for cls in section.classes:
            story.append(Paragraph(_esc(cls.class_name), styles["h2"]))
            story.append(_entries_table(cls.entries, styles))
            story.append(Spacer(1, 0.3 * cm))

    doc.build(story)
    return buf.getvalue()


def _entries_table(
    entries: Iterable, styles: dict[str, ParagraphStyle]
) -> Table:
    """
    Формирует Table из participant-строк. Без вложенной функции код
    рендера каталога раздувается — выносим, чтобы не мешать чтению.
    """
    rows = [
        [
            Paragraph("№", styles["small"]),
            Paragraph("Кличка", styles["small"]),
            Paragraph("Дата рожд.", styles["small"]),
            Paragraph("Окрас", styles["small"]),
            Paragraph("РКФ", styles["small"]),
            Paragraph("Владелец", styles["small"]),
            Paragraph("Питомник", styles["small"]),
        ]
    ]
    for e in entries:
        rows.append(
            [
                Paragraph(
                    f"{e.catalog_number:03d}" if e.catalog_number else "—",
                    styles["body"],
                ),
                # Все строки от пользователя (кличка, окрас, имена) —
                # через _esc, чтобы Paragraph не падал на "<", "&" и т. п.
                Paragraph(_esc(e.dog_name) or "—", styles["body"]),
                Paragraph(
                    e.date_of_birth.strftime("%d.%m.%Y") if e.date_of_birth else "—",
                    styles["body"],
                ),
                Paragraph(_esc(e.color) or "—", styles["body"]),
                Paragraph(_esc(e.rkf_number) or "—", styles["body"]),
                Paragraph(_esc(e.owner_name) or "—", styles["body"]),
                Paragraph(_esc(e.breeder_name) or "—", styles["body"]),
            ]
        )
    table = Table(rows, colWidths=[1.2 * cm, 4 * cm, 2 * cm, 2.5 * cm, 2 * cm, 3 * cm, 3 * cm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


# ---------------------------------------------------------------------
# Диплом
# ---------------------------------------------------------------------


def render_diploma(data: DiplomaData) -> bytes:
    """
    Один диплом на одну страницу A4. Минималистичная вёрстка: шапка,
    блок собаки, блок результата, подпись судьи.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"Диплом {data.dog_name}",
    )
    styles = _make_styles()
    story: list = []

    story.append(Paragraph("ДИПЛОМ", styles["title"]))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(_esc(data.show_name), styles["h1"]))
    story.append(
        Paragraph(
            f"Ранг: {_esc(data.show_rank)} &nbsp; • &nbsp; "
            f"Дата: {data.date_start.strftime('%d.%m.%Y')}"
            + (f" &nbsp; • &nbsp; Город: {_esc(data.city)}" if data.city else ""),
            styles["body"],
        )
    )
    story.append(Spacer(1, 1 * cm))

    story.append(Paragraph(f"Порода: {_esc(data.breed_name)}", styles["body"]))
    story.append(Paragraph(f"Класс: {_esc(data.class_name)}", styles["body"]))
    story.append(Spacer(1, 0.4 * cm))

    # Кличка/имена/титулы — пользовательские. <b>/<i>/markup ставим в
    # шаблоне САМИ, а в подставляемые значения попадают только эскейпленные
    # строки (иначе ввод <i>Bobby</i> ломал бы XML-парсер Paragraph'а).
    story.append(
        Paragraph(f"Кличка: <b>{_esc(data.dog_name)}</b>", styles["body"])
    )
    if data.rkf_number:
        story.append(
            Paragraph(f"РКФ №: {_esc(data.rkf_number)}", styles["body"])
        )
    if data.owner_name:
        story.append(
            Paragraph(f"Владелец: {_esc(data.owner_name)}", styles["body"])
        )
    story.append(Spacer(1, 0.8 * cm))

    if data.grade_name:
        story.append(
            Paragraph(
                f"Оценка: <b>{_esc(data.grade_name)}</b>", styles["body"]
            )
        )
    if data.placement is not None:
        story.append(
            Paragraph(f"Место: <b>{_esc(data.placement)}</b>", styles["body"])
        )
    if data.titles:
        story.append(
            Paragraph(
                "Титулы: <b>"
                + ", ".join(_esc(t) for t in data.titles)
                + "</b>",
                styles["body"],
            )
        )
    story.append(Spacer(1, 1.5 * cm))

    if data.judge_name:
        story.append(
            Paragraph(
                f"Судья: <i>{_esc(data.judge_name)}</i>", styles["body"]
            )
        )

    doc.build(story)
    return buf.getvalue()


def render_diplomas_batch(items: Iterable[DiplomaData]) -> bytes:
    """
    Несколько дипломов в одном PDF — через PageBreak. Удобно для печати
    "всех дипломов выставки" одной кнопкой.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = _make_styles()
    story: list = []
    items_list = list(items)
    for i, d in enumerate(items_list):
        # Каждый диплом — отдельная страница. Здесь мы не вызываем
        # render_diploma напрямую (он сам строит SimpleDocTemplate),
        # а строим элементы в общую story. PageBreak между ними.
        story.append(Paragraph("ДИПЛОМ", styles["title"]))
        story.append(Paragraph(_esc(d.show_name), styles["h1"]))
        story.append(
            Paragraph(
                f"{_esc(d.show_rank)} • {d.date_start.strftime('%d.%m.%Y')}"
                + (f" • {_esc(d.city)}" if d.city else ""),
                styles["body"],
            )
        )
        story.append(Spacer(1, 0.8 * cm))
        story.append(Paragraph(f"Порода: {_esc(d.breed_name)}", styles["body"]))
        story.append(Paragraph(f"Класс: {_esc(d.class_name)}", styles["body"]))
        story.append(
            Paragraph(f"Кличка: <b>{_esc(d.dog_name)}</b>", styles["body"])
        )
        if d.rkf_number:
            story.append(
                Paragraph(f"РКФ №: {_esc(d.rkf_number)}", styles["body"])
            )
        if d.grade_name:
            story.append(
                Paragraph(
                    f"Оценка: <b>{_esc(d.grade_name)}</b>", styles["body"]
                )
            )
        if d.titles:
            story.append(
                Paragraph(
                    "Титулы: <b>"
                    + ", ".join(_esc(t) for t in d.titles)
                    + "</b>",
                    styles["body"],
                )
            )
        if d.judge_name:
            story.append(Spacer(1, 1 * cm))
            story.append(
                Paragraph(
                    f"Судья: <i>{_esc(d.judge_name)}</i>", styles["body"]
                )
            )
        if i + 1 < len(items_list):
            story.append(PageBreak())
    doc.build(story)
    return buf.getvalue()
