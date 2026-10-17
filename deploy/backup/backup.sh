#!/bin/bash
# Бэкап Show Ring: PostgreSQL (pg_dump -Fc) + зеркало MinIO + ротация
# + опциональный offsite-S3. Расписание задаёт crond (см. entrypoint.sh),
# ручной запуск: docker compose run --rm backup backup.sh
#
# Обязательные env: POSTGRES_DB/USER/PASSWORD, S3_ENDPOINT/ACCESS_KEY/
#   SECRET_KEY/BUCKET (внутренний MinIO).
# Опциональные: BACKUP_KEEP_DAILY (7), BACKUP_KEEP_WEEKLY (4),
#   BACKUP_S3_* (все четыре заданы -> выгрузка offsite),
#   BACKUP_AGE_RECIPIENT (публичный ключ age -> шифрование, см. ниже).
#
# Шифрование (аудит безопасности 03.10.2026, № 11). В дампе телефоны,
# email и хеши паролей. С BACKUP_AGE_RECIPIENT:
# - дамп сразу шифруется (*.dump.age), открытая копия на диске не остаётся;
# - раз в неделю (вс) — зашифрованный архив файлов MinIO (files/*.tar.age);
# - offsite получает ТОЛЬКО зашифрованное: открытое зеркало minio/ туда
#   не уходит.
# Приватный ключ на сервере не хранится — он нужен только для restore.sh.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-4}"
RECIPIENT="${BACKUP_AGE_RECIPIENT:-}"
STAMP="$(date +%F)"   # YYYY-MM-DD
DOW="$(date +%u)"     # 1=пн .. 7=вс

log() { echo "[backup $(date -Iseconds)] $*"; }

mkdir -p "$BACKUP_DIR/postgres" "$BACKUP_DIR/minio"

if [ -n "$RECIPIENT" ]; then
    EXT=".dump.age"
    log "encryption on (age)"
else
    EXT=".dump"
    log "encryption off (BACKUP_AGE_RECIPIENT not set)"
fi

# --- 1. PostgreSQL: custom format (-Fc) — сжат, позволяет селективный
# restore отдельных таблиц через pg_restore -t.
DUMP="$BACKUP_DIR/postgres/show-ring-$STAMP$EXT"
# Открытый дамп пишем во временный файл рядом с целевым и удаляем при
# любом исходе (trap), чтобы при шифровании он не пережил скрипт.
RAW="$(mktemp "$BACKUP_DIR/postgres/.tmp-XXXXXX")"
trap 'rm -f "$RAW"' EXIT
log "pg_dump -> $DUMP"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$RAW"

# Smoke-проверка: архив читается. Молча битый дамп хуже отсутствующего —
# с ним живёшь в ложной уверенности, что бэкап есть.
pg_restore --list "$RAW" > /dev/null

if [ -n "$RECIPIENT" ]; then
    age -r "$RECIPIENT" "$RAW" > "$DUMP"
else
    mv "$RAW" "$DUMP"
fi
log "dump verified: $(du -h "$DUMP" | cut -f1)"

# Воскресный дамп получает weekly-копию (живёт дольше, см. ротацию).
if [ "$DOW" = "7" ]; then
    cp "$DUMP" "$BACKUP_DIR/postgres/weekly-show-ring-$STAMP$EXT"
    log "weekly copy created"
fi

# --- 2. Ротация: храним KEEP_DAILY свежих дневных и KEEP_WEEKLY недельных.
# ls -1t сортирует по mtime (новые сверху), tail -n +N — всё после N-го.
# Глоб show-ring-* НЕ матчит weekly-show-ring-* (имя должно начинаться
# с show-ring-), поэтому политики не пересекаются. Шифрованные и открытые
# дампы ротируются вместе — после включения шифрования старые открытые
# уходят по мере появления новых.
rotate() {
    # $1 — глоб-префикс, $2 — сколько файлов хранить, $3… — расширения.
    # Несматчившийся глоб (например, weekly-дампов ещё нет) роняет ls,
    # а под set -euo pipefail — весь скрипт. Пустой список — норма,
    # поэтому листинг гасим (|| true), но САМО удаление не маскируем:
    # упавший rm обязан завалить бэкап громко.
    local prefix="$1" keep="$2" old patterns=()
    shift 2
    for ext in "$@"; do
        patterns+=("$prefix"*"$ext")
    done
    old="$(ls -1t "${patterns[@]}" 2>/dev/null | tail -n +"$((keep + 1))")" || true
    if [ -n "$old" ]; then
        echo "$old" | xargs rm -f
    fi
}
rotate "$BACKUP_DIR/postgres/show-ring-" "$KEEP_DAILY" .dump .dump.age
rotate "$BACKUP_DIR/postgres/weekly-show-ring-" "$KEEP_WEEKLY" .dump .dump.age
log "rotation done (daily=$KEEP_DAILY, weekly=$KEEP_WEEKLY)"

# --- 3. MinIO: инкрементальное зеркало бакета (копируются только
# новые/изменённые объекты). --overwrite — обновлять изменившиеся.
# Зеркало локальное и открытое — как и сами файлы в MinIO на этом хосте.
mc alias set src "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" --api S3v4
mc mirror --overwrite "src/$S3_BUCKET" "$BACKUP_DIR/minio/$S3_BUCKET"
log "minio mirrored"

# Шифрованный снимок файлов для offsite: раз в неделю (полный архив
# каждый день раздувал бы облако).
if [ -n "$RECIPIENT" ]; then
    mkdir -p "$BACKUP_DIR/files"
    FILES="$BACKUP_DIR/files/show-ring-files-$STAMP.tar.age"
    if [ "$DOW" = "7" ] || ! ls "$BACKUP_DIR/files/"*.tar.age > /dev/null 2>&1; then
        tar -C "$BACKUP_DIR/minio" -cf - . | age -r "$RECIPIENT" > "$FILES"
        log "files archived: $(du -h "$FILES" | cut -f1)"
    fi
    rotate "$BACKUP_DIR/files/show-ring-files-" "$KEEP_WEEKLY" .tar.age
fi

# --- 4. Offsite (опционально): включается заданием всех четырёх
# BACKUP_S3_*. Не заданы — пропускаем с info-логом (осознанное решение
# спеки: сначала локально, облако добавляется четырьмя строками в .env).
if [ -n "${BACKUP_S3_ENDPOINT:-}" ] && [ -n "${BACKUP_S3_ACCESS_KEY:-}" ] \
    && [ -n "${BACKUP_S3_SECRET_KEY:-}" ] && [ -n "${BACKUP_S3_BUCKET:-}" ]; then
    mc alias set offsite "$BACKUP_S3_ENDPOINT" \
        "$BACKUP_S3_ACCESS_KEY" "$BACKUP_S3_SECRET_KEY" --api S3v4
    if [ -n "$RECIPIENT" ]; then
        # Только зашифрованное: дампы и архивы файлов.
        mc mirror --overwrite --exclude "*.dump" \
            "$BACKUP_DIR/postgres" "offsite/$BACKUP_S3_BUCKET/postgres"
        mc mirror --overwrite \
            "$BACKUP_DIR/files" "offsite/$BACKUP_S3_BUCKET/files"
    else
        mc mirror --overwrite "$BACKUP_DIR" "offsite/$BACKUP_S3_BUCKET"
    fi
    log "offsite uploaded -> $BACKUP_S3_BUCKET"
else
    log "offsite skipped (BACKUP_S3_* not set)"
fi

log "backup complete"
