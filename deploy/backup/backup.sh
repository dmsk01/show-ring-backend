#!/bin/bash
# Бэкап ShowTail: PostgreSQL (pg_dump -Fc) + зеркало MinIO + ротация
# + опциональный offsite-S3. Расписание задаёт crond (см. entrypoint.sh),
# ручной запуск: docker compose run --rm backup backup.sh
#
# Обязательные env: POSTGRES_DB/USER/PASSWORD, S3_ENDPOINT/ACCESS_KEY/
#   SECRET_KEY/BUCKET (внутренний MinIO).
# Опциональные: BACKUP_KEEP_DAILY (7), BACKUP_KEEP_WEEKLY (4),
#   BACKUP_S3_* (все четыре заданы -> выгрузка offsite).
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-4}"
STAMP="$(date +%F)"   # YYYY-MM-DD
DOW="$(date +%u)"     # 1=пн .. 7=вс

log() { echo "[backup $(date -Iseconds)] $*"; }

mkdir -p "$BACKUP_DIR/postgres" "$BACKUP_DIR/minio"

# --- 1. PostgreSQL: custom format (-Fc) — сжат, позволяет селективный
# restore отдельных таблиц через pg_restore -t.
DUMP="$BACKUP_DIR/postgres/showtail-$STAMP.dump"
log "pg_dump -> $DUMP"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$DUMP"

# Smoke-проверка: архив читается. Молча битый дамп хуже отсутствующего —
# с ним живёшь в ложной уверенности, что бэкап есть.
pg_restore --list "$DUMP" > /dev/null
log "dump verified: $(du -h "$DUMP" | cut -f1)"

# Воскресный дамп получает weekly-копию (живёт дольше, см. ротацию).
if [ "$DOW" = "7" ]; then
    cp "$DUMP" "$BACKUP_DIR/postgres/weekly-showtail-$STAMP.dump"
    log "weekly copy created"
fi

# --- 2. Ротация: храним KEEP_DAILY свежих дневных и KEEP_WEEKLY недельных.
# ls -1t сортирует по mtime (новые сверху), tail -n +N — всё после N-го.
# Глоб showtail-* НЕ матчит weekly-showtail-* (имя должно начинаться
# с showtail-), поэтому политики не пересекаются.
rotate() {
    # $1 — глоб-префикс, $2 — сколько файлов хранить.
    # Несматчившийся глоб (например, weekly-дампов ещё нет) роняет ls,
    # а под set -euo pipefail — весь скрипт. Пустой список — норма,
    # поэтому листинг гасим (|| true), но САМО удаление не маскируем:
    # упавший rm обязан завалить бэкап громко.
    local old
    old="$(ls -1t "$1"*.dump 2>/dev/null | tail -n +"$(($2 + 1))")" || true
    if [ -n "$old" ]; then
        echo "$old" | xargs rm -f
    fi
}
rotate "$BACKUP_DIR/postgres/showtail-" "$KEEP_DAILY"
rotate "$BACKUP_DIR/postgres/weekly-showtail-" "$KEEP_WEEKLY"
log "rotation done (daily=$KEEP_DAILY, weekly=$KEEP_WEEKLY)"

# --- 3. MinIO: инкрементальное зеркало бакета (копируются только
# новые/изменённые объекты). --overwrite — обновлять изменившиеся.
mc alias set src "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" --api S3v4
mc mirror --overwrite "src/$S3_BUCKET" "$BACKUP_DIR/minio/$S3_BUCKET"
log "minio mirrored"

# --- 4. Offsite (опционально): включается заданием всех четырёх
# BACKUP_S3_*. Не заданы — пропускаем с info-логом (осознанное решение
# спеки: сначала локально, облако добавляется четырьмя строками в .env).
if [ -n "${BACKUP_S3_ENDPOINT:-}" ] && [ -n "${BACKUP_S3_ACCESS_KEY:-}" ] \
    && [ -n "${BACKUP_S3_SECRET_KEY:-}" ] && [ -n "${BACKUP_S3_BUCKET:-}" ]; then
    mc alias set offsite "$BACKUP_S3_ENDPOINT" \
        "$BACKUP_S3_ACCESS_KEY" "$BACKUP_S3_SECRET_KEY" --api S3v4
    mc mirror --overwrite "$BACKUP_DIR" "offsite/$BACKUP_S3_BUCKET"
    log "offsite uploaded -> $BACKUP_S3_BUCKET"
else
    log "offsite skipped (BACKUP_S3_* not set)"
fi

log "backup complete"
