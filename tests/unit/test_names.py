# tests/unit/test_names.py
from types import SimpleNamespace

from app.utils.names import full_name, judge_display


def _user(email, profile=None):
    return SimpleNamespace(email=email, profile=profile)


def _profile(last=None, first=None, patr=None, country=None):
    return SimpleNamespace(
        last_name=last, first_name=first, patronymic=patr, country=country
    )


def test_full_name_all_parts():
    u = _user("a@b.c", _profile("Иванов", "Иван", "Иванович"))
    assert full_name(u) == "Иванов Иван Иванович"


def test_full_name_partial_skips_empty():
    u = _user("a@b.c", _profile("Иванов", "Иван", None))
    assert full_name(u) == "Иванов Иван"


def test_full_name_falls_back_to_email_when_no_profile():
    assert full_name(_user("a@b.c", None)) == "a@b.c"


def test_full_name_falls_back_when_profile_empty():
    assert full_name(_user("a@b.c", _profile())) == "a@b.c"


def test_full_name_none_user_returns_empty():
    assert full_name(None) == ""


def test_judge_display_with_country():
    u = _user("j@b.c", _profile("Никитина", "Ольга", country="Россия"))
    assert judge_display(u) == "Никитина Ольга (Россия)"


def test_judge_display_without_country():
    u = _user("j@b.c", _profile("Никитина", "Ольга"))
    assert judge_display(u) == "Никитина Ольга"
