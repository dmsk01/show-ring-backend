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
    RingSheetInput,
    RingRowInput,
)


def test_shape_ring_sheet_rows_and_blank_columns():
    sheet = _shape_ring_sheet(
        RingSheetInput(
            city="г. Москва",
            date="13.07.2025",
            judge="Никитина Ольга (Россия)",
            breed="Австралийская овчарка",
            ring_number=1,
            class_name="класс щенков",
            sex="male",
            rows=[
                RingRowInput(
                    catalog_number=1,
                    dog_name="Bobby",
                    date_of_birth="01.03.2024",
                    color="блю-мерль",
                    pedigree="RKF1",
                    tattoo="T1",
                    microchip="C1",
                    breeder="Сидорова Анна",
                    owner="Петров Пётр",
                ),
            ],
        )
    )
    assert sheet["sex"] == "кобели"
    assert sheet["ring_number"] == "1"
    row = sheet["rows"][0]
    assert row["catalog_number"] == "1"
    assert "Bobby" in row["name_dob_color"]
    assert "01.03.2024" in row["name_dob_color"]
    assert "RKF1" in row["pedigree_marks"]
    assert "Сидорова Анна" in row["breeder_owner"]
    assert "Петров Пётр" in row["breeder_owner"]


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
    assert ctx["total_entries"] == 2


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
