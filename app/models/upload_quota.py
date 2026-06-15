"""
Модель редактируемых лимитов квот загрузки файлов (по тирам).

Источник истины для лимитов — эта таблица в PostgreSQL (не Redis):
сам счётчик квоты тоже считается в БД (upload_quota repository), держим
всё в одном месте — единая консистентность и независимость от Redis.
3 строки (untrusted/standard/breeder) засеяны миграцией; админ правит их
через PUT /admin/upload-quotas/{tier}.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UploadQuotaTier(Base):
    __tablename__ = "upload_quota_tiers"

    # tier — строковый PK, совпадает со значениями UploadTier enum
    # (app/services/upload_quota.py). Набор фиксирован, create/delete не
    # предусмотрены — только чтение и обновление известных строк.
    tier: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Сколько загрузок в сутки (скользящее окно 24ч).
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # Потолок суммарного объёма всех файлов юзера, в байтах.
    max_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
