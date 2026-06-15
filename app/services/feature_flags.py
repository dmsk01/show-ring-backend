"""
Feature flags — скрытие неготового функционала за рантайм-переключателями.

Источник истины — Redis (ключи ff:{name}), с фоллбэком на дефолты из env
(класс FeatureFlags, env_prefix "FF_"). Дефолт у всех флагов False: пока
фичу явно не включили, она выглядит несуществующей.

Почему не отдельная таблица в PG: флаги читаются на КАЖДОМ запросе через
require_flag — это hot-path, и round-trip в БД тут лишний. Redis уже
подключён в проекте (rate-limit, idempotency, ad-dedup), переиспользуем
его. Поверх Redis — крошечный in-memory кеш (CACHE_TTL), чтобы не ходить
в Redis на каждый запрос: значения флагов меняются редко, отставание в
пару секунд после переключения админом некритично.

FlagService — синглтон (см. flag_service внизу): in-memory кеш должен быть
общим для всех запросов, поэтому НЕ создаём его per-request в Depends.
Redis-клиент сервис читает лениво (redis_state.redis_client), как и
остальные сервисы проекта, — поэтому в lifespan его регистрировать не
нужно, он работает поверх уже инициализированного init_redis() клиента.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import Depends, HTTPException, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import redis as redis_state

logger = logging.getLogger(__name__)


class FeatureFlags(BaseSettings):
    """
    Дефолтные значения флагов. Имена полей = множество ИЗВЕСТНЫХ флагов:
    FlagService.known_flags берёт их отсюда, и записать произвольный ключ
    в Redis через API нельзя (валидируем по этому списку).

    Все флаги bool, по умолчанию False — неготовая фича скрыта, пока её
    явно не включат через env (FF_<NAME>=true) или рантайм-переключатель.

    Добавление нового флага = добавить поле сюда. Больше нигде менять не
    надо: эндпойнты, кеш и валидация автоматически подхватят новое поле.
    """

    # extra="ignore": .env содержит непрефиксованные ключи основного
    # Settings (secret_key, database_url, ...). Без ignore pydantic счёл бы
    # их «лишними» и упал — нас интересуют только переменные с префиксом FF_.
    model_config = SettingsConfigDict(
        env_prefix="FF_", env_file=".env", extra="ignore"
    )

    # Официальные документы РКФ (диплом/ринговка/каталог по DOCX-шаблонам) —
    # спецификация и план есть, реализация в работе.
    official_documents: bool = False
    # Авторизация по телефону через SMS-OTP — конфиг заведён, фича дозревает.
    phone_otp_auth: bool = False


class FlagService:
    """Чтение/запись флагов поверх Redis с in-memory кешем."""

    # TTL in-memory кеша. ~2 секунды: компромисс между «не ходить в Redis
    # на каждый запрос» и «быстро увидеть переключение, сделанное админом».
    CACHE_TTL_SECONDS = 2.0
    KEY_PREFIX = "ff:"

    def __init__(self, defaults: FeatureFlags) -> None:
        self._defaults = defaults
        # Кеш — целый снапшот всех флагов, а не по ключу: одно обращение в
        # Redis (MGET) обновляет сразу всё, а is_enabled/all() читают из
        # одного словаря. None = кеш пуст/инвалидирован.
        self._cache: dict[str, bool] | None = None
        self._cache_at = 0.0
        # Защита от «стампеды»: при истечении кеша только одна корутина
        # идёт в Redis, остальные ждут готовый снапшот.
        self._lock = asyncio.Lock()

    @property
    def known_flags(self) -> tuple[str, ...]:
        return tuple(FeatureFlags.model_fields.keys())

    def _default(self, name: str) -> bool:
        return bool(getattr(self._defaults, name))

    async def is_enabled(self, name: str) -> bool:
        """Включён ли флаг. Неизвестное имя → False (защита от опечаток)."""
        snapshot = await self._snapshot()
        # name может не быть в snapshot, если запросили незнакомый флаг —
        # трактуем как выключенный, а не как KeyError.
        return snapshot.get(name, False)

    async def all(self) -> dict[str, bool]:
        """Все известные флаги с актуальными значениями (для фронтенда)."""
        # Копия, чтобы наружу не утёк внутренний кеш-объект.
        return dict(await self._snapshot())

    async def set(self, name: str, enabled: bool) -> None:
        """
        Записать значение флага в Redis. Имя обязано быть известным —
        иначе ValueError (роутер мапит в 404): не даём плодить «мусорные»
        ключи ff:* мимо FeatureFlags.
        """
        if name not in self.known_flags:
            raise ValueError("unknown_flag")
        client = redis_state.redis_client
        if client is None:
            # Без Redis записать некуда — пусть вызывающий вернёт 503.
            raise RuntimeError("redis_unavailable")
        await client.set(f"{self.KEY_PREFIX}{name}", "1" if enabled else "0")
        # Инвалидируем кеш, чтобы следующее чтение увидело новое значение
        # (а не ждало истечения CACHE_TTL).
        self._cache = None

    # -- внутреннее ----------------------------------------------------

    async def _snapshot(self) -> dict[str, bool]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < self.CACHE_TTL_SECONDS:
            return self._cache
        async with self._lock:
            # double-check: пока ждали лок, другая корутина могла обновить.
            now = time.monotonic()
            if (
                self._cache is not None
                and now - self._cache_at < self.CACHE_TTL_SECONDS
            ):
                return self._cache
            snapshot = await self._load_from_redis()
            self._cache = snapshot
            self._cache_at = now
            return snapshot

    async def _load_from_redis(self) -> dict[str, bool]:
        names = self.known_flags
        # Базис — дефолты из env: фоллбэк, если ключа в Redis нет или Redis
        # недоступен (fail-open к дефолтам, как ad-dedup).
        result = {name: self._default(name) for name in names}
        client = redis_state.redis_client
        if client is None:
            return result
        try:
            keys = [f"{self.KEY_PREFIX}{name}" for name in names]
            # MGET — одно обращение на все флаги вместо N GET'ов.
            values = await client.mget(keys)
            for name, raw in zip(names, values):
                if raw is not None:
                    result[name] = raw == "1"
        except Exception as e:  # noqa: BLE001 — fail-open к дефолтам при сбое Redis
            logger.warning("FlagService: Redis read failed, using defaults: %s", e)
        return result


# Синглтон: общий in-memory кеш на весь процесс. DI отдаёт именно его.
flag_service = FlagService(FeatureFlags())


def get_flag_service() -> FlagService:
    """DI-зависимость: единый экземпляр FlagService."""
    return flag_service


def require_flag(name: str):
    """
    Гейт роута за флагом. Если флаг выключен — 404, а не 403: выключенная
    фича должна выглядеть несуществующей, а не «запрещённой» (не палим
    наличие неготового функционала). Вешается в dependencies роута/роутера.
    """

    async def dependency(
        service: FlagService = Depends(get_flag_service),
    ) -> None:
        if not await service.is_enabled(name):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    return dependency
