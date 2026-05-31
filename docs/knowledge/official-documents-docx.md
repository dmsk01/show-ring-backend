# Официальные документы выставки через DOCX-шаблоны

## Зачем DOCX-шаблоны, а не ReportLab

Документы РКФ (диплом, ринговая ведомость, каталог) имеют сложное
фиксированное оформление: рамки, двуязычные блоки, точные шрифты и
расположение. Повторить это программно в ReportLab — дорого и неточно.
Поэтому исходный документ становится **шаблоном Word (.docx)**: оформление
сохраняется 1-в-1, а бэкенд лишь подставляет данные.

Старый функционал генерации PDF на ReportLab (этап 8, `app/utils/pdf.py`)
**не тронут** — официальные документы реализованы параллельно.

## Стек

- **`docxtpl`** (поверх `python-docx` + Jinja2) — подстановка данных в
  .docx-шаблон.

Вывод только **.docx**. PDF намеренно не делаем: точная конвертация
.docx→PDF требует офисного движка (LibreOffice/Word) — тяжёлую зависимость
не тащим. Готовый .docx при необходимости сохраняется в PDF из Word вручную.

## Поток данных

```
POST /shows/{id}/official/<doc>
  → создаётся Task(pending), публикуется в RabbitMQ (очередь document_task)
  → воркер: process_document_task → ветка по task.type
      → document_official.build_*_context(db, ...)   # сбор данных из БД
      → docx_render.render_docx(template, context)    # .docx (bytes)
      → file_storage.upload_bytes(...) в MinIO
      → mark_done(file_id)
  → клиент опрашивает GET /tasks/{id}, затем GET /tasks/{id}/download
```

Блокирующий рендер в воркере уводится в `asyncio.to_thread`, чтобы не
вешать event loop.

## Где что лежит

| Файл | Ответственность |
|------|-----------------|
| `app/templates/documents/*.docx` | шаблоны (поставляются вручную; см. README рядом) |
| `app/services/document_official.py` | сбор контекста из БД + чистые шейперы |
| `app/utils/docx_render.py` | `render_docx` (только .docx) |
| `app/utils/names.py` | `full_name`, `judge_display` (ФИО, страна) |
| `worker/handlers/document_handler.py` | ветки `*_OFFICIAL`, `_render_official` |
| `app/routers/documents.py` | ручки `/official/*`, `/context`, `/readiness` |

## Паттерн «шейпер + билдер»

Каждый документ собирается в два слоя:
- **`build_*_context(db, ...)`** — грузит ORM-объекты, резолвит имена.
- **`_shape_*(...)`** — чистая функция: из простых значений строит словарь
  для docxtpl. Тестируется без БД (`tests/unit/test_official_context.py`),
  что важно: локальный PostgreSQL часто выключен.

## Разделение заводчик / владелец

Документам нужны разные люди:
- **владелец** — `Dog.kennel.owner` (текущий питомник, меняется при продаже);
- **заводчик** — `Dog.breeder_kennel.owner` (питомник рождения, неизменен),
  с fallback на free-text `Dog.breeder_name` для импортных собак.

ФИО берутся из новой таблицы `user_profiles` (1:1 к `users`:
`last_name/first_name/patronymic/country`). Если профиль пуст —
`full_name` отдаёт email как fallback.

## Как добавить новый документ

1. Положить `.docx`-шаблон в `app/templates/documents/` с плейсхолдерами
   docxtpl (см. `app/templates/documents/README.md` про теги
   `{{ }}`, `{%tr%}`, `{%p%}`, `{% for %}`).
2. Добавить `build_<doc>_context` (+ чистый `_shape_<doc>`) в
   `document_official.py` и unit-тест на шейпер.
3. Добавить значение в `DocumentKind` и ветку-хендлер в
   `worker/handlers/document_handler.py`.
4. Добавить ручку в `app/routers/documents.py`.
5. Добавить smoke-тест в `tests/unit/test_official_templates.py`
   (он сам пропустится, пока .docx не добавлен).

## Удобство фронта

- `GET /shows/{id}/official/{kind}/context` — отдаёт собранные данные
  документа в JSON (предпросмотр/правка до генерации).
- `GET /shows/{id}/documents/readiness` — чек-лист пробелов (нет номера
  каталога, ФИО владельца/заводчика, клейма+чипа, родословной), чтобы
  организатор дозаполнил данные до печати.
