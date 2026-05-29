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
        )
    )
    assert ctx["show_name"] == "WORLD DOG SHOW 2025"
    assert ctx["judge"] == "Никитина Ольга (Россия)"
    assert ctx["sex_male"] is True
    assert ctx["sex_female"] is False
    assert ctx["dob"] == "01.03.2024"
    assert ctx["place"] == "1"
    assert ctx["dog_name"] == "Bobby vom Haus"
    assert ctx["pedigree"] == "RKF1234567"


def test_shape_diploma_empty_fields_become_blank_strings():
    ctx = _shape_diploma_context(
        DiplomaInput(
            show_name="X",
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
    assert ctx["grade"] == ""
    assert ctx["dob"] == ""
    assert ctx["place"] == ""
    assert ctx["judge"] == ""
