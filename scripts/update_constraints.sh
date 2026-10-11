#!/bin/bash
# ============================================================
# Обновление constraints.txt — точных версий зависимостей (BE-29)
# ============================================================
#
# Ставит requirements-dev.txt в чистый python:3.12-slim (как прод-образ,
# Linux) и записывает pip freeze. Запускать из корня репозитория после
# изменения requirements*.txt или для планового обновления версий:
#
#   ./scripts/update_constraints.sh
#
# Потом: собрать образ, прогнать тесты, закоммитить constraints.txt.
# Установка ~100 пакетов в чистый контейнер идёт несколько минут.
# ============================================================

set -euo pipefail

cd "$(dirname "$0")/.."

{
  sed -n '1,8p' constraints.txt
  echo
  docker run --rm -v "$PWD:/src:ro" -w /src python:3.12-slim sh -c \
    "pip install -q --root-user-action=ignore --disable-pip-version-check -r requirements-dev.txt \
     && pip freeze --exclude-editable"
} > constraints.txt.new

mv constraints.txt.new constraints.txt
echo "constraints.txt обновлён: $(grep -c '==' constraints.txt) пакетов"
