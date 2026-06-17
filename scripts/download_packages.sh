#!/bin/bash
# ============================================================
# Скачивание всех пакетов Show Ring для офлайн-установки
# ============================================================
#
# Использование:
#   1. На машине с интернетом:
#      chmod +x scripts/download_packages.sh
#      ./scripts/download_packages.sh
#
#   2. Перенести папку offline_packages/ на офлайн-машину
#
#   3. На офлайн-машине:
#      pip install --no-index --find-links=offline_packages/ -r requirements.txt
#      pip install --no-index --find-links=offline_packages/ -r requirements-dev.txt
#
# ============================================================

set -e

PACKAGES_DIR="offline_packages"
PYTHON_VERSION=$(python3 --version 2>/dev/null || python --version)

echo "========================================"
echo " Show Ring — Скачивание пакетов для офлайн"
echo " Python: $PYTHON_VERSION"
echo " Папка: $PACKAGES_DIR/"
echo "========================================"

# Очистить предыдущую папку
rm -rf "$PACKAGES_DIR"
mkdir -p "$PACKAGES_DIR"

echo ""
echo "[1/3] Скачивание production-зависимостей..."
pip download -r requirements.txt -d "$PACKAGES_DIR" --no-cache-dir

echo ""
echo "[2/3] Скачивание dev-зависимостей..."
pip download -r requirements-dev.txt -d "$PACKAGES_DIR" --no-cache-dir

echo ""
echo "[3/3] Скачивание Docker-образов (опционально)..."
echo "  Для скачивания Docker-образов выполните:"
echo ""
echo "  docker pull postgres:16"
echo "  docker pull rabbitmq:3-management"
echo "  docker pull redis:7-alpine"
echo "  docker pull minio/minio"
echo "  docker pull axllent/mailpit"
echo ""
echo "  docker save postgres:16 rabbitmq:3-management redis:7-alpine minio/minio axllent/mailpit -o $PACKAGES_DIR/docker_images.tar"
echo ""
echo "  На офлайн-машине:"
echo "  docker load -i $PACKAGES_DIR/docker_images.tar"

# Подсчитать размер
TOTAL_SIZE=$(du -sh "$PACKAGES_DIR" | cut -f1)
TOTAL_FILES=$(ls -1 "$PACKAGES_DIR" | wc -l)

echo ""
echo "========================================"
echo " Готово!"
echo " Файлов: $TOTAL_FILES"
echo " Размер: $TOTAL_SIZE"
echo " Папка:  $PACKAGES_DIR/"
echo "========================================"
echo ""
echo " Для установки на офлайн-машине:"
echo "   pip install --no-index --find-links=$PACKAGES_DIR/ -r requirements.txt"
echo "   pip install --no-index --find-links=$PACKAGES_DIR/ -r requirements-dev.txt"
echo "========================================"
