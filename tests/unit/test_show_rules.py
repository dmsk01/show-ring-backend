"""
Unit-тесты правил РКФ (этап 13).

Тестируем функции, которые не требуют БД:
- age_in_months_on — расчёт возраста собаки на дату выставки;
- _class_matches_age — попадание возраста в диапазон класса;
- is_transition_allowed — корректность статус-машины выставки.

Базовая логика проекта — здесь критично, что эти функции не ломаются
при рефакторинге. Стоимость тестов низкая (нет фикстур, нет I/O).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.models.show import ShowStatus
from app.services.show_rules import (
    ALLOWED_TRANSITIONS,
    _class_matches_age,
    age_in_months_on,
    is_transition_allowed,
)


# ---------------------------------------------------------------------
# age_in_months_on
# ---------------------------------------------------------------------


class TestAgeInMonths:
    def test_exact_year(self):
        # 1 января 2024 → 1 января 2025 = ровно 12 месяцев.
        assert age_in_months_on(date(2024, 1, 1), date(2025, 1, 1)) == 12

    def test_same_day_zero(self):
        # День рождения — возраст 0 месяцев (новорожденному «0 мес»,
        # а не 1).
        assert age_in_months_on(date(2025, 6, 15), date(2025, 6, 15)) == 0

    def test_before_birthday(self):
        # Если день месяца ещё не настал — минус месяц.
        # 1 января 2024 → 31 марта 2025 = 14 месяцев (а не 15).
        # Февраль/март: 2025-01 - 2024-01 = 12 + (3-1) = 14, день 31>1 → ок.
        assert age_in_months_on(date(2024, 1, 5), date(2025, 3, 4)) == 13

    def test_after_birthday_in_month(self):
        # 5 января 2024 → 6 января 2025 = 12 месяцев (день 6 ≥ 5).
        assert age_in_months_on(date(2024, 1, 5), date(2025, 1, 6)) == 12

    def test_future_date_returns_zero(self):
        # on_date раньше date_of_birth — возраст 0 (защита от логических
        # ошибок: ребёнок в будущем).
        assert age_in_months_on(date(2025, 6, 1), date(2024, 1, 1)) == 0

    @pytest.mark.parametrize(
        "dob,on_date,expected",
        [
            # Бэби (4-6 мес): 5-месячный щенок.
            (date(2025, 1, 1), date(2025, 6, 1), 5),
            # Юниор (9-18 мес): 14 месяцев.
            (date(2024, 1, 1), date(2025, 3, 1), 14),
            # Ветеран (96+ мес): 9 лет = 108 месяцев.
            (date(2016, 1, 1), date(2025, 1, 1), 108),
        ],
    )
    def test_typical_dog_ages(self, dob, on_date, expected):
        assert age_in_months_on(dob, on_date) == expected


# ---------------------------------------------------------------------
# _class_matches_age
# ---------------------------------------------------------------------


def _cls(age_from: int, age_to: int | None) -> SimpleNamespace:
    """Минимальный «фейковый» ShowClass для unit-теста — без БД."""
    return SimpleNamespace(age_from_months=age_from, age_to_months=age_to)


class TestClassMatchesAge:
    def test_too_young(self):
        # Бэби 4-6 мес: 3-месячному щенку не подходит.
        assert _class_matches_age(_cls(4, 6), 3) is False

    def test_too_old(self):
        # Бэби 4-6 мес: 7-месячному не подходит.
        assert _class_matches_age(_cls(4, 6), 7) is False

    def test_in_range_inclusive(self):
        # Граница включительная — 4 и 6 месяцев подходят в бэби.
        assert _class_matches_age(_cls(4, 6), 4) is True
        assert _class_matches_age(_cls(4, 6), 6) is True

    def test_open_class_no_upper_bound(self):
        # age_to_months=None → открытый класс (15+ месяцев без верха).
        cls = _cls(15, None)
        assert _class_matches_age(cls, 15) is True
        assert _class_matches_age(cls, 200) is True
        assert _class_matches_age(cls, 14) is False

    def test_overlapping_age_in_multiple_classes(self):
        # 15 мес попадает и в юниоров (9-18), и в промежуточный (15-24).
        # Это базовая «двусмысленность» РКФ — владелец сам выбирает класс.
        assert _class_matches_age(_cls(9, 18), 15) is True
        assert _class_matches_age(_cls(15, 24), 15) is True


# ---------------------------------------------------------------------
# Статус-машина выставки
# ---------------------------------------------------------------------


class TestStatusTransitions:
    def test_draft_to_registration_open(self):
        assert is_transition_allowed(
            ShowStatus.draft, ShowStatus.registration_open
        )

    def test_draft_cannot_jump_to_in_progress(self):
        # Прямой переход draft → in_progress запрещён: регистрация
        # должна быть пройдена.
        assert not is_transition_allowed(
            ShowStatus.draft, ShowStatus.in_progress
        )

    def test_completed_is_terminal(self):
        # Из completed выйти нельзя — это финальное состояние.
        assert ALLOWED_TRANSITIONS[ShowStatus.completed] == set()

    def test_cancelled_is_terminal(self):
        assert ALLOWED_TRANSITIONS[ShowStatus.cancelled] == set()

    def test_cancel_allowed_from_any_active_state(self):
        # Отменить можно из draft, reg_open, reg_closed, in_progress.
        for src in (
            ShowStatus.draft,
            ShowStatus.registration_open,
            ShowStatus.registration_closed,
            ShowStatus.in_progress,
        ):
            assert is_transition_allowed(src, ShowStatus.cancelled), src
