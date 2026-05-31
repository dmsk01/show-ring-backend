# Сертификаты титулов (формат РКФ) — Design

**Дата:** 2026-05-31
**Статус:** утверждён, реализуется.

## Цель

Генерировать сертификаты титулов выставки в том же конвейере, что диплом /
ринговая ведомость / каталог (этап 8): DOCX-шаблон + `docxtpl` → MinIO →
скачивание. Без PDF (LibreOffice из проекта убран).

## Решения (из брейншторма)

- **Образца РКФ нет** → шаблон `certificate.docx` собирается программно
  (python-docx), оформление наше.
- **Гранулярность:** один сертификат = пара (собака, титул). Собака с CAC и
  ЛПП → два сертификата, по странице на каждый.
- **Набор титулов:** все титулы из `ShowResult.titles_cache` (что присуждено).
  Фильтр «только сертификатные коды» не делаем (YAGNI).
- **Вывод:** только DOCX.

## Архитектура

Зеркалит официальные документы:

```
POST /shows/{id}/official/certificates            (батч по выставке)
POST /shows/{id}/entries/{eid}/official/certificates   (одна собака)
  → Task(pending) → RabbitMQ (document_task)
  → воркер: _handle_certificates_official
      → build_certificates_context(db, show_id, entry_id=None)
      → render_docx("certificate.docx", ctx)
      → MinIO → mark_done(file_id)
  → GET /tasks/{id}, GET /tasks/{id}/download
```

### Контекст — `app/services/document_official.py`

- `build_certificates_context(db, show_id, entry_id=None)`:
  идём по `ShowEntry` (всех или одной), берём `ShowResult`, и для каждого
  титула в `result.titles_cache` собираем один сертификат.
  Переиспользуем `_resolve_owner`, `_resolve_breeder`, `_load_user_with_profile`,
  `judge_display`, `_fmt_date_long`, `_s`.
- Чистый `_shape_certificate(CertificateInput) -> dict` (тест без БД).
- Поля сертификата: `title`, `dog_name`, `breed_line` («(FCI N) ПОРОДА»),
  `catalog_number`, `pedigree`, `owner`, `breeder`, `show_title`
  (название + ранг), `date` (длинная), `city`, `judge`.
- Возврат: `{"certificates": [<cert>, ...]}`. Пустой список, если титулов нет.

### Шаблон — `app/templates/documents/certificate.docx`

Собирается скриптом из python-docx. Центрированный блок на страницу:
заголовок «РОССИЙСКАЯ КИНОЛОГИЧЕСКАЯ ФЕДЕРАЦИЯ / FCI» → крупно `{{ c.title }}`
→ `{{ c.dog_name }}` → `{{ c.breed_line }}` → `№ {{ c.catalog_number }},
{{ c.pedigree }}` → «Владелец: {{ c.owner }}» / «Заводчик: {{ c.breeder }}» →
«{{ c.show_title }}, {{ c.city }}, {{ c.date }}» → «Судья: {{ c.judge }}» →
статичная строка «Подпись / печать».
Весь блок обёрнут `{%p for c in certificates %} … {%p endfor %}`, разрыв
страницы между сертификатами через `{% if not loop.last %}` (как в ведомости).

### Воркер — `worker/handlers/document_handler.py`

`DocumentKind.CERTIFICATES_OFFICIAL = "generate_certificates_official"`;
ветка `_handle_certificates_official(db, payload, created_by)` →
`build_certificates_context` → `_render_official("certificate.docx", ...)`.

### Ручки — `app/routers/documents.py`

Две ручки (как у диплома): батч по выставке и по одной записи; обе кладут
`{"show_id": ...}` (+ `{"entry_id": ...}`) в payload и публикуют
`CERTIFICATES_OFFICIAL`. Доступ — организатор/админ (`_ensure_organizer`).

## Тесты

- `test_official_context`: `_shape_certificate` + сборка из 2 титулов → 2
  сертификата с верными полями.
- `test_official_templates`: рендер `certificate.docx` на тест-контексте
  (PK, размер > 2000, без остатков Jinja).

## Вне scope

Экспортная карточка РКФ, фильтр сертификатных кодов, PDF.
