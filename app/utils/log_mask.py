"""
Маскирование персональных данных в логах (ревью 2026-10-06, BE-12).

Логи уходят в агрегатор и живут дольше, чем допускает 152-ФЗ для email и
телефонов в открытом виде. Для корреляции событий достаточно маски:
первый символ адреса + домен, код страны + последние 4 цифры номера.
"""

from __future__ import annotations


def mask_email(email: str | None) -> str:
    if not email:
        return "-"
    local, sep, domain = email.strip().partition("@")
    if not sep or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def mask_phone(phone: str | None) -> str:
    if not phone:
        return "-"
    phone = phone.strip()
    if len(phone) < 8:
        return "***"
    return f"{phone[:2]}{'*' * (len(phone) - 6)}{phone[-4:]}"
