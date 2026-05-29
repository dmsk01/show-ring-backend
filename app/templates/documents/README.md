# DOCX-шаблоны официальных документов РКФ

Сюда кладутся **бинарные .docx-шаблоны**, созданные из RTF-образцов РКФ и
размеченные плейсхолдерами `docxtpl`. Рендер — `app/utils/docx_render.py`,
сбор данных — `app/services/document_official.py`.

Ожидаемые файлы:

| Файл | Назначение | Контекст-билдер |
|------|-----------|-----------------|
| `diploma.docx` | диплом одного участника | `build_diploma_context` |
| `diplomas_batch.docx` | все дипломы выставки в одном файле | `build_diplomas_batch_context` → `{"diplomas": [<ctx диплома>, ...]}` |
| `ring_sheet.docx` | ринговые ведомости | `build_ring_sheets_context` → `{"sheets": [<sheet>, ...]}` |
| `catalog.docx` | каталог выставки | `build_catalog_context` |

## Плейсхолдеры

### diploma.docx
Поля (вписывать как обычный текст в нужную графу):
`{{ show_name }}`, `{{ judge }}`, `{{ breed }}`, `{{ class_name }}`,
`{{ grade }}`, `{{ title }}`, `{{ place }}`, `{{ dog_name }}`,
`{{ tattoo }}`, `{{ microchip }}`, `{{ dob }}`, `{{ owner }}`,
`{{ kennel }}`, `{{ breeder }}`, `{{ pedigree }}`.
Отметка пола: возле «КОБЕЛИ/MALES» — `{% if sex_male %}X{% endif %}`;
возле «СУКИ/FEMALES» — `{% if sex_female %}X{% endif %}`.

### diplomas_batch.docx
Скопировать diploma.docx, обернуть всё содержимое в
`{% for d in diplomas %}` … `{% endfor %}`, заменить `{{ field }}` →
`{{ d.field }}` (например `{{ d.show_name }}`), добавить разрыв страницы
в конце цикла.

### ring_sheet.docx
Весь блок одного листа (шапка + таблица) обернуть в
`{% for sheet in sheets %}` … `{% endfor %}`, внутри — разрыв страницы.
Шапка: `{{ sheet.city }}`, `{{ sheet.date }}`, `{{ sheet.judge }}`,
`{{ sheet.breed }}`, `{{ sheet.ring_number }}`, `{{ sheet.class_name }}`,
`{{ sheet.sex }}`.
Строка таблицы участников — повторяемая через docxtpl row-теги: в первой
ячейке строки-образца `{%tr for row in sheet.rows %}`, в последней
`{%tr endfor %}`. Ячейки: `{{ row.catalog_number }}`,
`{{ row.name_dob_color }}`, `{{ row.pedigree_marks }}`,
`{{ row.breeder_owner }}`, и пустые (судья пишет от руки) `{{ row.grade }}`,
`{{ row.titles }}`, `{{ row.place }}`, `{{ row.litter }}`, `{{ row.total }}`.

### catalog.docx
Шапка: `{{ show_name }}`, `{{ show_rank }}`, `{{ period }}`, `{{ city }}`,
`{{ venue }}`, `{{ total_entries }}`.
Судьи — повтор абзаца: `{%p for j in judges %}` …
`{{ j.name }} — {{ j.assignment }}` … `{%p endfor %}`.
Тело — вложенные абзацные циклы `{%p ... %}`:
`{%p for group in groups %}` → `Группа FCI {{ group.group_number }}. {{ group.group_name }}`
  `{%p for breed in group.breeds %}` → `{{ breed.breed_name }} (FCI {{ breed.fci_number }}). Судья: {{ breed.judge }}`
    `{%p for cls in breed.classes %}` → `{{ cls.class_name }} — {{ cls.sex }}`
      записи — таблица со строкой-циклом `{%tr for e in cls.entries %}` …
      `{%tr endfor %}`, ячейки: `{{ e.catalog_number }}`, `{{ e.dog_name }}`,
      `{{ e.dob }}`, `{{ e.color }}`, `{{ e.pedigree }}`, `{{ e.marks }}`,
      `{{ e.breeder }}`, `{{ e.owner }}`, `{{ e.sire }}`, `{{ e.dam }}`
    `{%p endfor %}` `{%p endfor %}` `{%p endfor %}`

## Проверка
`tests/unit/test_official_templates.py` рендерит каждый шаблон на тестовом
контексте. Пока файла .docx нет — тест пропускается (skip). После добавления
шаблонов тесты активируются автоматически.

ВАЖНО: вписывая тег, следите, чтобы Word не разбил его на несколько «runs»
(из-за автозамены/орфографии). Набирайте тег одним заходом; если docxtpl
ругается на сломанный тег — выделите его и перепечатайте.
