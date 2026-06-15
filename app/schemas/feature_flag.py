"""Схемы эндпойнтов feature flags."""

from __future__ import annotations

from pydantic import BaseModel


class FeatureFlagSetRequest(BaseModel):
    """Тело рантайм-переключателя: новое значение флага."""

    enabled: bool
