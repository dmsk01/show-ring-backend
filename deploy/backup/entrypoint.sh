#!/bin/sh
# Без аргументов — режим демона: crontab из $BACKUP_CRON + crond.
# С аргументами — выполняем их (ручной запуск:
#   docker compose run --rm backup backup.sh
#   docker compose run --rm backup restore.sh <dump> [db]).
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# stdout cron-задачи перенаправляем в stdout PID 1 — логи видны
# в `docker compose logs backup`.
echo "${BACKUP_CRON:-30 3 * * *} /usr/local/bin/backup.sh >> /proc/1/fd/1 2>&1" \
    > /etc/crontabs/root
echo "[backup] crond started, schedule: ${BACKUP_CRON:-30 3 * * *}"
exec crond -f -l 8
