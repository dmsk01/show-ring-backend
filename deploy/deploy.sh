#!/usr/bin/env bash
#
# Автодеплой Show Ring: обновляет оба репозитория (бэк + соседний фронт) и
# пересобирает прод-стек. Запускается:
#   - вручную:           <бэк>/deploy/deploy.sh
#   - из GitHub Actions: self-hosted runner на этом же сервере
#     (.github/workflows/ci.yml, job deploy) на каждый push в main.
#
# Идемпотентно — гонять можно сколько угодно. Образы собираются ЛОКАЛЬНО
# (как в docker-compose.prod.yml), реестр не нужен; Docker кеширует слои,
# поэтому неизменившийся сервис пересборку пропускает.
set -euo pipefail

# Сериализуем параллельные деплои: push почти одновременно в оба репо
# запустил бы два deploy.sh сразу, а GitHub concurrency между РАЗНЫМИ
# репозиториями не шарится. flock — вторая копия дождётся первую.
exec 9>/tmp/show-ring-deploy.lock
flock 9

# BACKEND_DIR определяем по расположению самого скрипта (<бэк>/deploy/
# deploy.sh) — работает независимо от того, куда склонирован репозиторий
# (/opt/show-ring-backend, /opt/show-ring-backend — без разницы).
# FRONTEND_DIR по умолчанию — соседний каталог show-ring-frontend
# (раскладка из docs/deployment-linux.md, шаг 7); переопределяется env-кой
# SHOW_RING_FRONTEND_DIR, если фронт лежит в другом месте.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
FRONTEND_DIR="${SHOW_RING_FRONTEND_DIR:-$(dirname "$BACKEND_DIR")/show-ring-frontend}"

# Прод-связка compose-файлов + .env (с секретами) лежит в бэкенд-каталоге.
compose() {
    docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env "$@"
}

echo "==> [1/4] Обновляю репозитории (ff-only; рабочее дерево на сервере должно быть чистым)"
git -C "$FRONTEND_DIR" pull --ff-only
git -C "$BACKEND_DIR" pull --ff-only

cd "$BACKEND_DIR"

echo "==> [2/4] Пересобираю образы и поднимаю стек (миграции накатит контейнер migrate)"
compose up -d --build

echo "==> [3/4] Убираю повисшие образы прошлых сборок"
docker image prune -f

echo "==> [4/4] Текущий статус:"
compose ps
