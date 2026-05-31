# tests/unit/test_official_templates.py
"""
Smoke-тесты рендера DOCX-шаблонов официальных документов.

Шаблоны (app/templates/documents/*.docx) поставляются вручную (созданы из
RTF-образцов РКФ и размечены плейсхолдерами docxtpl). Пока файла шаблона
нет, соответствующий тест пропускается (skip) — это позволяет держать
тесты в репозитории до появления шаблонов и автоматически их активировать,
когда .docx будут добавлены.

Что проверяем: рендер не падает на реальном контексте и отдаёт валидный
.docx (zip-сигнатура `PK`). Визуальное соответствие образцу проверяется
вручную — автотестом верстку не покрыть.
"""

import pytest

from app.utils import docx_render

TEMPLATES = docx_render.TEMPLATES_DIR


def _skip_if_absent(name: str) -> None:
    if not (TEMPLATES / name).exists():
        pytest.skip(f"template {name} not provided yet")


def _diploma_ctx() -> dict:
    return {
        "show_name": "WORLD DOG SHOW 2025",
        "judge": "Никитина Ольга (Россия)",
        "breed": "Австралийская овчарка",
        "sex_male": True, "sex_female": False,
        "class_name": "класс щенков", "grade": "отлично",
        "title": "CW, ЛПП", "place": "1",
        "dog_name": "Bobby vom Haus", "tattoo": "ABC123",
        "microchip": "643094100123456", "dob": "01.03.2024",
        "owner": "Петров Пётр", "kennel": "От Каховки",
        "breeder": "Сидорова Анна", "pedigree": "RKF1234567",
    }


def _ring_ctx() -> dict:
    return {
        "sheets": [
            {
                "organizer": "МОО КПС Красный Маяк",
                "show_title": "Красный Маяк ранга CAC",
                "breed": "Русский чёрный терьер",
                "judge": "Мордвинова Татьяна Александровна",
                "date": "22 ноября 2025 г.",
                "ring_number": "1",
                "numbers": ["20", "68", "69"],
                "numbers_str": "20, 68, 69",
            }
        ]
    }


def _catalog_ctx() -> dict:
    return {
        "show_name": "Региональная выставка ранга САС",
        "show_rank": "САС", "period": "13.07.2025",
        "city": "Москва", "venue": "Крокус", "total_entries": 1,
        "judges": [{"name": "Судья А (Россия)", "assignment": "группа FCI 1"}],
        "groups": [
            {
                "group_number": "1", "group_name": "Овчарки",
                "breeds": [
                    {
                        "breed_name": "Австралийская овчарка",
                        "fci_number": "342", "judge": "Судья А (Россия)",
                        "classes": [
                            {
                                "class_name": "класс щенков", "sex": "суки",
                                "entries": [
                                    {
                                        "catalog_number": "1",
                                        "dog_name": "Bella",
                                        "dob": "02.02.2024", "color": "мерль",
                                        "pedigree": "RKF1", "marks": "T / C",
                                        "breeder": "Зав", "owner": "Вл",
                                        "sire": "Отец", "dam": "Мать",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_diploma_template_renders():
    _skip_if_absent("diploma.docx")
    body = docx_render.render_docx("diploma.docx", _diploma_ctx())
    assert body[:2] == b"PK"
    assert len(body) > 2000


def test_ring_sheet_template_renders():
    _skip_if_absent("ring_sheet.docx")
    body = docx_render.render_docx("ring_sheet.docx", _ring_ctx())
    assert body[:2] == b"PK"
    assert len(body) > 2000


def test_catalog_template_renders():
    _skip_if_absent("catalog.docx")
    body = docx_render.render_docx("catalog.docx", _catalog_ctx())
    assert body[:2] == b"PK"
    assert len(body) > 2000


def test_diplomas_batch_template_renders():
    _skip_if_absent("diplomas_batch.docx")
    ctx = {"diplomas": [_diploma_ctx(), _diploma_ctx()]}
    body = docx_render.render_docx("diplomas_batch.docx", ctx)
    assert body[:2] == b"PK"
    assert len(body) > 2000


def _certificate_ctx() -> dict:
    cert = {
        "title": "CAC", "dog_name": "Rex",
        "breed_line": "(FCI 143) Доберман", "catalog_number": "10",
        "pedigree": "RKF10", "owner": "Вл", "breeder": "Зав",
        "show_title": "Выставка ранга САС", "date": "22 ноября 2025 г.",
        "city": "Москва", "judge": "Судья А",
    }
    return {"certificates": [cert, {**cert, "title": "ЛПП"}]}


def test_certificate_template_renders():
    _skip_if_absent("certificate.docx")
    body = docx_render.render_docx("certificate.docx", _certificate_ctx())
    assert body[:2] == b"PK"
    assert len(body) > 2000
