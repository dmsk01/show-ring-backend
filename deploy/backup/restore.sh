#!/bin/bash
# Восстановление Show Ring из дампа.
#   docker compose run --rm backup restore.sh /backups/postgres/<file>.dump [dbname]
#
# По умолчанию восстанавливает в $POSTGRES_DB (боевую!). Для учения
# восстановления передавайте отдельную БД вторым аргументом:
#   docker compose exec postgres createdb -U show_ring show_ring_restore
#   docker compose run --rm backup restore.sh /backups/postgres/<f>.dump show_ring_restore
#
# Зашифрованный дамп (*.dump.age, см. BACKUP_AGE_RECIPIENT в backup.sh)
# расшифровывается приватным ключом age. Ключ на сервере не хранится —
# подключаем его только на время восстановления:
#   docker compose run --rm -v /путь/к/backup-key.txt:/run/age-key.txt:ro \
#     backup restore.sh /backups/postgres/<f>.dump.age show_ring_restore
# (путь к ключу внутри контейнера меняется через AGE_IDENTITY).
#
# Файлы MinIO восстанавливаются зеркалом в обратную сторону:
#   docker compose run --rm backup mc alias set src http://minio:9000 <key> <secret>
#   docker compose run --rm backup mc mirror --overwrite /backups/minio/<bucket> src/<bucket>
# Из зашифрованного архива (files/*.tar.age) — сначала распаковать:
#   age -d -i /run/age-key.txt <архив>.tar.age | tar -C /tmp/files -xf -
set -euo pipefail

DUMP="${1:?usage: restore.sh <dump-file> [dbname]}"
TARGET_DB="${2:-$POSTGRES_DB}"

if [[ "$DUMP" == *.age ]]; then
    IDENTITY="${AGE_IDENTITY:-/run/age-key.txt}"
    if [ ! -r "$IDENTITY" ]; then
        echo "[restore] нет ключа age: $IDENTITY (см. шапку restore.sh)" >&2
        exit 1
    fi
    PLAIN="$(mktemp /tmp/restore-XXXXXX)"
    trap 'rm -f "$PLAIN"' EXIT
    age -d -i "$IDENTITY" "$DUMP" > "$PLAIN"
    echo "[restore] decrypted $DUMP"
    DUMP="$PLAIN"
fi

echo "[restore] $DUMP -> db '$TARGET_DB'"
# --clean --if-exists: дропнуть объекты перед созданием (идемпотентно);
# --no-owner: не пытаться выставлять владельца из дампа (на VPS он
# совпадает, но это делает дамп переносимым между окружениями).
PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
    --clean --if-exists --no-owner \
    -h postgres -U "$POSTGRES_USER" -d "$TARGET_DB" "$DUMP"
echo "[restore] done"
