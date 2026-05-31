# Асинхронная обработка фото (варианты + watermark) — Design

**Дата:** 2026-05-31
**Статус:** утверждён, реализуется.

## Цель

При загрузке изображения генерировать уменьшенные варианты (превью + средний
с водяным знаком) асинхронно через воркер — реализовать `file_handler` из
плана Этапа 8.

## Решения (из брейншторма)

- **Скоуп:** и ресайз-варианты, и watermark.
- **Триггер:** любая загрузка `image/*` через `POST /files` → задача
  `process_image(file_id)`.
- **Хранение вариантов:** отдельная таблица `file_variants` (1:N к
  `uploaded_files`).
- **Варианты:** `thumb` (макс. сторона 200px, без wm), `medium` (макс. 1000px,
  с wm). Оригинал — как есть. Watermark = текст «ShowTail», полупрозрачный,
  правый нижний угол. Выход вариантов — JPEG.

## Архитектура (зеркалит документы)

```
POST /files (image/*)
  → UploadedFile (синхронно, как сейчас)
  → Task(type="process_image", payload={"file_id": ...}) + publish image_task
  → возвращаем файл (варианты ещё не готовы)

воркер (--mode files): on_image_message → process_image_task(db, task_id)
  → грузим Task → file_id → UploadedFile → скачиваем оригинал из MinIO
  → для каждого варианта: image_processing.make_variant(bytes, max_size, wm?)
  → upload_bytes в MinIO (key "variants/{file_id}/{kind}.jpg")
  → INSERT FileVariant
  → mark_done(result={"variants": [<id>...]})
```

### Модель — `app/models/file.py`

`FileVariant(Base)`:
- `id` UUID PK
- `file_id` FK → `uploaded_files.id` (CASCADE), index
- `kind` String(32) — "thumb" / "medium"
- `s3_key` String(512) unique
- `content_type` String(128)
- `width`, `height` Integer
- `has_watermark` Boolean
- `size_bytes` BigInteger
- `created_at` TIMESTAMPTZ
- UNIQUE(`file_id`, `kind`)

`UploadedFile.variants` — relationship (1:N, cascade delete-orphan).
Миграция Alembic: `create_table file_variants`.

### Утилита — `app/utils/image_processing.py` (чистая, тестируется без БД/MinIO)

`make_variant(image_bytes, max_size: int, watermark_text: str | None) ->
(jpeg_bytes, width, height)`:
- Pillow: open → EXIF-transpose → конверт в RGB → `thumbnail((max,max))`
  (сохраняет пропорции, не увеличивает) → опц. watermark (полупрозрачный текст
  в правом нижнем углу) → save JPEG (quality ~85).
- Конфиг вариантов: `VARIANTS = [("thumb", 200, False), ("medium", 1000, True)]`.

### Воркер — `worker/handlers/file_handler.py` + `worker/main.py`

- `process_image_task(db, task_id)` — см. поток. Идемпотентность: перед
  генерацией удаляем прежние `FileVariant` файла (+ их объекты в MinIO).
- `worker/main.py`: `IMAGE_TASK_QUEUE = "image_task"`, `run_files()` +
  `on_image_message` (по образцу `run_documents`/`on_document_message`),
  ветка `--mode files`.
- `docker-compose.yml`: сервис `worker-files` (`--mode files`).

### API — `app/routers/files.py`

- `POST /files`: после сохранения `UploadedFile`, если `content_type`
  начинается с `image/`, создаём Task + публикуем в `image_task` (как делает
  роутер документов через task_repo + rabbit_service).
- `GET /files/{id}`: в ответ добавляем `variants: [{kind, width, height,
  has_watermark, url/download}]` (или их id для скачивания через существующий
  `/files/{variant_id}`... — варианты тоже `UploadedFile`? нет, отдельная
  таблица; отдаём `variant_id` + ссылку на новый `GET /files/variants/{id}`
  для скачивания потока из MinIO).

## Тесты

- `test_image_processing`: `make_variant` на сгенерированной Pillow-картинке
  (напр. 2000×1500) → thumb ≤200 по большей стороне, medium ≤1000, оба JPEG
  (`\xff\xd8`), watermark-вариант отличается по байтам/не падает.
- (опц.) контейнерный e2e: загрузка картинки → дождаться `file_variants`.

## Вне scope

Кроп по лицу, webp/avif, CDN, ресайз на лету.
