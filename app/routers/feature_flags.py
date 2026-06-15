"""
Feature flags API.

- GET /feature-flags         — публичный: фронтенд узнаёт, что показывать.
- PUT /feature-flags/{name}  — только admin: переключить флаг в рантайме.

Чтения флага для гейта роутов тут НЕТ — это делает require_flag
(app/services/feature_flags.py), навешиваемый в dependencies нужного роута.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import require_any_role
from app.schemas.feature_flag import FeatureFlagSetRequest
from app.services.feature_flags import FlagService, get_flag_service

router = APIRouter(prefix="/feature-flags", tags=["feature-flags"])


@router.get("", response_model=dict[str, bool])
async def list_feature_flags(
    service: FlagService = Depends(get_flag_service),
) -> dict[str, bool]:
    """Все известные флаги с актуальными значениями."""
    return await service.all()


# Переключатель — под админ-ролью (как admin/references). Путь /{name}
# идемпотентен: PUT нужного значения, повтор безопасен.
@router.put(
    "/{name}",
    response_model=dict[str, bool],
    dependencies=[Depends(require_any_role("admin"))],
)
async def set_feature_flag(
    name: str,
    body: FeatureFlagSetRequest,
    service: FlagService = Depends(get_flag_service),
) -> dict[str, bool]:
    """
    Записать значение флага. Имя валидируется по FeatureFlags: неизвестный
    флаг → 404 (нельзя писать произвольные ключи). Возвращаем полный
    снапшот — админ-UI сразу видит свежее состояние.
    """
    try:
        await service.set(name, body.enabled)
    except ValueError:
        # unknown_flag — такого флага нет в FeatureFlags.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown feature flag"
        )
    except RuntimeError:
        # redis_unavailable — записать состояние некуда.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis недоступен",
        )
    return await service.all()
