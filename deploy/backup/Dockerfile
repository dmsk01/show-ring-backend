# Backup-контейнер Show Ring: pg_dump + mc (MinIO client) + busybox crond.
#
# База — postgres:17-alpine: pg_dump ТОЙ ЖЕ мажорной версии, что сервер.
# Несовпадение версий клиента/сервера — классическая причина дампов,
# которые «делались годами», а при restore оказались несовместимыми.
#
# mc берём из закреплённого образа форка (pgsty/mc, тег + digest): официальный
# dl.min.io отвечает 410, а скачивание «последней версии» без проверки
# контрольной суммы было дырой в цепочке поставки (аудит 03.10.2026, № 11).
# Бинарник статический — работает и на alpine.
FROM pgsty/mc:RELEASE.2026-09-16T00-00-00Z@sha256:cfc83108c3abb371f8fb84d99c1fdc88f8c237e022409b0081fb7c0a3be634dd AS mc

FROM postgres:17-alpine

# age — шифрование дампов (BACKUP_AGE_RECIPIENT, аудит 03.10.2026, № 11).
RUN apk add --no-cache bash age
COPY --from=mc /usr/bin/mc /usr/local/bin/mc

COPY backup.sh restore.sh entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh \
        /usr/local/bin/entrypoint.sh

# Переопределяем entrypoint postgres-образа (нам не нужен сервер БД,
# только клиентские утилиты + cron).
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
