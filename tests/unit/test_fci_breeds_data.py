"""
Гард на форму данных справочника пород (scripts/data/fci_breeds.py).

Список курируется вручную и регулярно пополняется — тест ловит
механические ошибки правок: пропущенное поле в кортеже, пустой перевод,
дубль кода, опечатку в номере группы.
"""

from __future__ import annotations

from scripts.data.fci_breeds import FCI_BREEDS


def test_rows_have_five_fields() -> None:
    for row in FCI_BREEDS:
        assert len(row) == 5, f"строка не 5-кортеж: {row!r}"


def test_groups_in_fci_range() -> None:
    assert all(1 <= row[0] <= 10 for row in FCI_BREEDS)


def test_codes_unique() -> None:
    codes = [row[1] for row in FCI_BREEDS]
    duplicates = {c for c in codes if codes.count(c) > 1}
    assert not duplicates, f"дубли кодов пород: {duplicates}"


def test_names_non_empty_and_localized() -> None:
    for _group, code, name_ru, name_en, _fci in FCI_BREEDS:
        assert name_ru.strip(), f"{code}: пустое русское имя"
        assert name_en.strip(), f"{code}: пустое английское имя"
        # Английское имя должно быть ASCII — see решение в docstring модуля.
        assert name_en.isascii(), f"{code}: name_en не ASCII: {name_en!r}"
