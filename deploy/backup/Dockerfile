# Backup-контейнер ShowTail: pg_dump + mc (MinIO client) + busybox crond.
#
# База — postgres:17-alpine: pg_dump ТОЙ ЖЕ мажорной версии, что сервер.
# Несовпадение версий клиента/сервера — классическая причина дампов,
# которые «делались годами», а при restore оказались несовместимыми.
FROM postgres:17-alpine

RUN apk add --no-cache bash curl \
    && curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc \
        -o /usr/local/bin/mc \
    && chmod +x /usr/local/bin/mc

COPY backup.sh restore.sh entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh \
        /usr/local/bin/entrypoint.sh

# Переопределяем entrypoint postgres-образа (нам не нужен сервер БД,
# только клиентские утилиты + cron).
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
