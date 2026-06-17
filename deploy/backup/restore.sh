#!/bin/bash
# Восстановление Show Ring из дампа.
#   docker compose run --rm backup restore.sh /backups/postgres/<file>.dump [dbname]
#
# По умолчанию восстанавливает в $POSTGRES_DB (боевую!). Для учения
# восстановления передавайте отдельную БД вторым аргументом:
#   docker compose exec postgres createdb -U show_ring show_ring_restore
#   docker compose run --rm backup restore.sh /backups/postgres/<f>.dump show_ring_restore
#
# Файлы MinIO восстанавливаются зеркалом в обратную сторону:
#   docker compose run --rm backup mc alias set src http://minio:9000 <key> <secret>
#   docker compose run --rm backup mc mirror --overwrite /backups/minio/<bucket> src/<bucket>
set -euo pipefail

DUMP="${1:?usage: restore.sh <dump-file> [dbname]}"
TARGET_DB="${2:-$POSTGRES_DB}"

echo "[restore] $DUMP -> db '$TARGET_DB'"
# --clean --if-exists: дропнуть объекты перед созданием (идемпотентно);
# --no-owner: не пытаться выставлять владельца из дампа (на VPS он
# совпадает, но это делает дамп переносимым между окружениями).
PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
    --clean --if-exists --no-owner \
    -h postgres -U "$POSTGRES_USER" -d "$TARGET_DB" "$DUMP"
echo "[restore] done"
