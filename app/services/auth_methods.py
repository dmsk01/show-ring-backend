"""
Реестр способов аутентификации.

Единая точка правды о том, какими способами можно войти и
зарегистрироваться. Клиент получает реестр через GET /auth/methods и рисует
экраны по нему, а не по захардкоженному списку — новый способ (VK ID,
Telegram и т.п.) добавляется сюда, без правок экранов входа.

Порядок в списке — порядок показа; первый метод — основной.
"""

from dataclasses import dataclass

from app.config import settings

PHONE_OTP = "phone_otp"
EMAIL_PASSWORD = "email_password"


@dataclass(frozen=True)
class AuthMethod:
    id: str
    sign_in: bool
    sign_up: bool


def enabled_auth_methods() -> list[AuthMethod]:
    methods = [
        # Телефон всегда включён: при закрытой email-регистрации это
        # единственный путь создать аккаунт — выключать его нельзя.
        AuthMethod(id=PHONE_OTP, sign_in=True, sign_up=True),
        AuthMethod(
            id=EMAIL_PASSWORD,
            sign_in=settings.auth_email_login_enabled,
            sign_up=settings.auth_email_registration_enabled,
        ),
    ]
    # Метод, которым нельзя ни войти, ни зарегистрироваться, клиенту не нужен.
    return [m for m in methods if m.sign_in or m.sign_up]


def primary_auth_method() -> str:
    return PHONE_OTP
