# tests/unit/test_official_context.py
import datetime as dt
from types import SimpleNamespace

from app.services.document_official import (
    _shape_diploma_context,
    DiplomaInput,
)


def test_shape_diploma_full():
    ctx = _shape_diploma_context(
        DiplomaInput(
            show_name="WORLD DOG SHOW 2025",
            show_rank="CAC",
            judge="Никитина Ольга (Россия)",
            breed="Австралийская овчарка",
            sex="male",
            class_name="класс щенков",
            grade="отлично",
            title="CW, ЛПП",
            placement=1,
            dog_name="Bobby vom Haus",
            tattoo="ABC123",
            microchip="643094100123456",
            date_of_birth=dt.date(2024, 3, 1),
            owner="Петров Пётр",
            kennel="От Каховки",
            breeder="Сидорова Анна",
            pedigree="RKF1234567",
            catalog_number=20,
            fci_number="342",
        )
    )
    assert ctx["show_name"] == "WORLD DOG SHOW 2025"
    assert ctx["judge"] == "Никитина Ольга (Россия)"
    assert ctx["sex_male"] is True
    assert ctx["sex_female"] is False
    assert ctx["sex_word"] == "Кобель"
    assert ctx["catalog_number"] == "20"
    assert ctx["show_rank"] == "CAC"
    assert ctx["breed_line"] == "(FCI 342) Австралийская овчарка"
    assert ctx["dob"] == "01.03.2024"
    assert ctx["place"] == "1"
    assert ctx["dog_name"] == "Bobby vom Haus"
    assert ctx["pedigree"] == "RKF1234567"


def test_shape_diploma_empty_fields_become_blank_strings():
    ctx = _shape_diploma_context(
        DiplomaInput(
            show_name="X",
            show_rank=None,
            judge=None,
            breed="Y",
            sex="female",
            class_name="откр.",
            grade=None,
            title=None,
            placement=None,
            dog_name="Z",
            tattoo=None,
            microchip=None,
            date_of_birth=None,
            owner=None,
            kennel=None,
            breeder=None,
            pedigree=None,
        )
    )
    assert ctx["sex_female"] is True
    assert ctx["sex_male"] is False
    assert ctx["sex_word"] == "Сука"
    assert ctx["catalog_number"] == ""
    assert ctx["grade"] == ""
    assert ctx["dob"] == ""
    assert ctx["place"] == ""
    assert ctx["judge"] == ""


from app.services.document_official import (
    _shape_ring_sheet,
    _fmt_date_long,
    RingSheetInput,
)


def test_fmt_date_long_russian_month():
    import datetime as _dt
    assert _fmt_date_long(_dt.date(2025, 11, 22)) == "22 ноября 2025 г."
    assert _fmt_date_long(None) == ""


def test_shape_ring_sheet_per_breed_blank():
    sheet = _shape_ring_sheet(
        RingSheetInput(
            organizer="МОО КПС Красный Маяк",
            show_title="Красный Маяк ранга CAC",
            breed="Русский чёрный терьер",
            judge="Мордвинова Татьяна Александровна",
            date="22 ноября 2025 г.",
            ring_number=1,
            catalog_numbers=[20, 68, None],
        )
    )
    assert sheet["organizer"] == "МОО КПС Красный Маяк"
    assert sheet["show_title"] == "Красный Маяк ранга CAC"
    assert sheet["breed"] == "Русский чёрный терьер"
    assert sheet["judge"] == "Мордвинова Татьяна Александровна"
    assert sheet["date"] == "22 ноября 2025 г."
    assert sheet["ring_number"] == "1"
    # None-номер не попадает в строку, но в список входит как пустой.
    assert sheet["numbers"] == ["20", "68", ""]
    assert sheet["numbers_str"] == "20, 68"


from app.services.document_official import (
    _shape_catalog,
    CatalogMeta,
    CatalogEntryInput,
)


def test_shape_catalog_groups_sorts_and_formats():
    meta = CatalogMeta(
        show_name="Выставка",
        show_rank="САС",
        period="13.07.2025",
        city="Москва",
        venue=None,
        judges=[{"name": "Судья А", "assignment": "группа FCI 1"}],
    )
    entries = [
        CatalogEntryInput(
            group_number=2, group_name="Пинчеры", breed_name="Доберман",
            fci_number="143", breed_judge="Судья Б",
            class_name="откр.", sex="male", catalog_number=10,
            dog_name="Rex", date_of_birth="01.01.2022", color="чёрный",
            pedigree="RKF10", tattoo="T", microchip="C",
            breeder="Зав1", owner="Вл1", sire="Отец", dam="Мать",
        ),
        CatalogEntryInput(
            group_number=1, group_name="Овчарки", breed_name="Аусси",
            fci_number="342", breed_judge="Судья А",
            class_name="щенков", sex="female", catalog_number=1,
            dog_name="Bella", date_of_birth="02.02.2024", color="мерль",
            pedigree="RKF1", tattoo=None, microchip=None,
            breeder="Зав2", owner="Вл2", sire=None, dam=None,
        ),
    ]
    ctx = _shape_catalog(meta, entries)
    assert ctx["show_name"] == "Выставка"
    assert [g["group_number"] for g in ctx["groups"]] == ["1", "2"]
    g1 = ctx["groups"][0]
    assert g1["breeds"][0]["breed_name"] == "Аусси"
    cls0 = g1["breeds"][0]["classes"][0]
    assert cls0["class_name"] == "щенков"
    assert cls0["entries"][0]["catalog_number"] == "1"
    assert cls0["entries"][0]["dog_name"] == "Bella"
    # Сводные поля породы (для таблицы «Породы по группам»).
    assert g1["breeds"][0]["entry_count"] == 1
    assert g1["breeds"][0]["catalog_range"] == "1"
    # detail_line: пустые поля (клеймо/чип, родители) выпадают без лишних запятых.
    assert (
        cls0["entries"][0]["detail_line"]
        == "RKF1, д.р. 02.02.2024, мерль, зав. Зав2, вл. Вл2"
    )
    assert ctx["total_entries"] == 2


from app.services.document_official import _shape_certificate, CertificateInput


def test_shape_certificate_builds_breed_line():
    cert = _shape_certificate(
        CertificateInput(
            title="CAC", dog_name="Rex", breed="Доберман", fci_number="143",
            catalog_number=10, pedigree="RKF10", owner="Вл", breeder="Зав",
            show_title="Выставка ранга САС", date="22 ноября 2025 г.",
            city="Москва", judge="Судья А",
        )
    )
    assert cert["title"] == "CAC"
    assert cert["dog_name"] == "Rex"
    assert cert["breed_line"] == "(FCI 143) Доберман"
    assert cert["catalog_number"] == "10"
    assert cert["pedigree"] == "RKF10"
    assert cert["owner"] == "Вл"
    assert cert["show_title"] == "Выставка ранга САС"
    assert cert["judge"] == "Судья А"


def test_shape_certificate_no_fci_and_empty_fields():
    cert = _shape_certificate(
        CertificateInput(
            title="ЛПП", dog_name="Bella", breed="Аусси", fci_number=None,
            catalog_number=None, pedigree=None, owner=None, breeder=None,
            show_title="X", date="", city=None, judge=None,
        )
    )
    assert cert["breed_line"] == "Аусси"  # без FCI — только название
    assert cert["catalog_number"] == ""
    assert cert["owner"] == ""
    assert cert["judge"] == ""


from app.services.document_official import _entry_issues, EntryCheck


def test_entry_issues_flags_missing():
    issues = _entry_issues(
        EntryCheck(
            catalog_number=None, dog_name="Rex",
            owner_present=False, breeder_present=False,
            has_tattoo=False, has_microchip=False, has_pedigree=False,
        )
    )
    codes = {i["code"] for i in issues}
    assert "no_catalog_number" in codes
    assert "no_owner" in codes
    assert "no_breeder" in codes
    assert "no_id" in codes  # ни клейма, ни чипа
    assert "no_pedigree" in codes


def test_entry_issues_clean():
    issues = _entry_issues(
        EntryCheck(
            catalog_number=1, dog_name="Rex",
            owner_present=True, breeder_present=True,
            has_tattoo=True, has_microchip=False, has_pedigree=True,
        )
    )
    assert issues == []
