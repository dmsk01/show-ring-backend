"""
Письма по подпискам содержат ссылку на управление подписками («Отписаться»),
транзакционные (подтверждение email, смена пароля) — нет: от них не
отписываются, они нужны для безопасности аккаунта.
"""

from app.config import settings
from app.services.email import render_email

SUBSCRIPTION_URL = f"{settings.frontend_base_url}/dashboard/notifications"


def test_subscription_email_has_unsubscribe_link():
    _, html, text = render_email(
        "litter.announced", {"kennel_name": "Тест", "breed_name": "Лабрадор"}
    )
    assert SUBSCRIPTION_URL in html
    assert "Отписаться" in html
    assert SUBSCRIPTION_URL in text


def test_transactional_email_has_no_unsubscribe_footer():
    _, html, text = render_email(
        "password_changed", {"email": "a@example.com"}
    )
    assert SUBSCRIPTION_URL not in html
    assert SUBSCRIPTION_URL not in text
