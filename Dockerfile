# syntax=docker/dockerfile:1.6
#
# Multi-stage build для Show Ring backend (этап 15).
#
# Почему multi-stage:
# - Builder ставит зависимости с компиляцией (gcc/wheel-build);
#   итоговый размер слоя нам не интересен, его дропаем.
# - Runtime использует slim-образ БЕЗ build-tool'ов. Меньше attack
#   surface и меньший образ к пушу.
#
# Один Dockerfile обслуживает три роли (api, worker, migrate) — на
# этапе 15 это команда CMD из docker-compose.yml, не отдельные образы.
# Когда понадобится разная зависимость для api/worker, разделим на
# два target'а.

# ---------------------------------------------------------------------
# Stage 1: builder
# ---------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Системные пакеты для сборки wheel'ов с C-расширениями
# (asyncpg, pydantic-core, reportlab — все они приходят с готовыми
# wheel'ами для x86_64-linux, но build-essential — страховка).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Виртуалка в /opt/venv. Через PATH её делаем активной для всех
# последующих pip/python вызовов.
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Сначала зависимости — этот слой кэшируется, пока requirements.txt
# не меняется. Код в этот слой не попадает, чтобы не инвалидировать
# кэш при каждом git pull.
WORKDIR /build
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Минимально нужные системные пакеты:
# - libpq5 — динамическая зависимость asyncpg в некоторых ситуациях;
# - curl — для HEALTHCHECK на /health;
# - procps — даёт pgrep, на который завязаны HEALTHCHECK'и воркеров
#   в docker-compose.yml (pgrep -f 'worker.main'). Без него проверка
#   падает с "command not found" и живой воркер помечается unhealthy.
# - ttf-dejavu — TTF-шрифт для PDF (см. app/utils/pdf.py, рендер
#   кириллицы).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        procps \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Копируем готовую виртуалку из builder'а.
ENV VIRTUAL_ENV=/opt/venv
COPY --from=builder $VIRTUAL_ENV $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Создаём non-root пользователя. Запуск процесса от root — лишний
# риск compromise. UID/GID 1000 — стандарт для "первого" пользователя
# на Debian, совместимо с большинством host-bind volumes.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --shell /bin/bash --home /app --create-home app

WORKDIR /app
COPY --chown=app:app . /app

USER app

# PYTHONDONTWRITEBYTECODE — не плодим .pyc в контейнере (read-only fs).
# PYTHONUNBUFFERED — print/log сразу в stdout, не теряются в буфере
# при kill контейнера.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Healthcheck для api-роли. Worker эту проверку не использует
# (compose-команда переопределяет healthcheck'и в worker-сервисе).
# Бьём в /health/ready (а НЕ /health): это бинарный readiness-probe —
# 200 если PG жив, 503 если БД down (app/routers/health.py). /health
# всегда отдаёт 200 (детальный отчёт для дашборда) и вдобавок 307-редиректит
# /health → /health/, из-за чего `curl -f` «проходил» на редиректе, не
# проверяя живость. /health/ready — точный путь, без редиректа.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/ready || exit 1

# CMD по умолчанию — uvicorn. docker-compose.yml переопределяет на
# alembic upgrade для migrate-контейнера и на python -m worker.main
# для воркера. Production: убрать --reload, использовать --workers=N.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
