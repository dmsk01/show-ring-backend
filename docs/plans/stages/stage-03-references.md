# Этап 3: Справочники

### Цель

Создать справочные таблицы (породы, классы выставок, ранги, титулы, оценки) и админский CRUD для управления ими. Заполнить начальными данными по РКФ.

### Что появляется в проекте

- Таблицы справочников: animal_types, breed_groups, breeds, show_classes, show_ranks, titles, grades
- Admin-only API для CRUD справочников
- Seed-скрипт с начальными данными (породы FCI, классы РКФ, титулы)
- Публичные GET-эндпоинты для чтения справочников (фильтрация, поиск)

### Модель данных

Новые таблицы:

**animal_types** — виды животных (dog, cat)
**breed_groups** — группы FCI (1-10 для собак)
**breeds** — породы (>350 пород FCI)
**show_classes** — классы: бэби (4-6м), щенки (6-9м), юниоры (9-18м), промежуточный (15-24м), открытый (15м+), рабочий (15м+), чемпионов (15м+), ветеранов (8л+)
**show_ranks** — ранги: CACIB, CAC ЧРКФ ОС, CAC ЧРКФ, CAC ЧФ, КЧК, ПК, ЧК
**titles** — титулы: CW, CAC, R.CAC, CACIB, R.CACIB, ЛК, ЛС, BOB, BIG, BIS, ЮСАС, ...
**grades** — оценки: отлично, очень хорошо, хорошо, удовлетворительно, дисквалификация; щенячьи: большая перспектива, перспективный, малоперспективный, неперспективный

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| GET | `/breeds` | Список пород (фильтр по animal_type, group) | Public |
| GET | `/breeds/{id}` | Порода подробно | Public |
| GET | `/show-classes` | Классы выставок | Public |
| GET | `/show-ranks` | Ранги выставок | Public |
| GET | `/titles` | Титулы | Public |
| GET | `/grades` | Оценки | Public |
| POST | `/admin/breeds` | Создать породу | Admin |
| PUT | `/admin/breeds/{id}` | Обновить породу | Admin |
| DELETE | `/admin/breeds/{id}` | Удалить породу | Admin |
| ... | `/admin/...` | Аналогично для всех справочников | Admin |

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/models/reference.py` | ORM-модели справочников |
| `app/schemas/reference.py` | Pydantic-схемы |
| `app/routers/references.py` | Публичные GET-эндпоинты |
| `app/routers/admin/references.py` | Admin CRUD |
| `app/repositories/reference.py` | SQL-запросы к справочникам |
| `app/services/reference.py` | Бизнес-логика (валидация при удалении) |
| `scripts/seed_references.py` | Заполнение начальными данными |

### Ключевые концепции

- **Seed data** — заполнение справочников через скрипт или Alembic data migration
- **Фильтрация и пагинация** — `GET /breeds?animal_type=dog&group=1&page=1&per_page=50`
- **Каскадное удаление** — нельзя удалить породу, если есть собаки этой породы
- **Admin guard** — все мутации только для role=admin

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| Foreign Keys, связи | breed → breed_group → animal_type |
| JOIN | Породы с группами: `SELECT b.*, bg.name FROM breeds b JOIN breed_groups bg ON ...` |
| Bulk INSERT | Seed-скрипт: вставка 350+ пород |
| WHERE + LIKE | Поиск пород по названию |
| Пагинация | LIMIT / OFFSET |
| Защита от удаления | EXISTS-подзапрос перед DELETE |

### Начальные данные (примеры)

**show_classes (для собак):**
| code | name | age_from | age_to | can_receive_cac |
|------|------|----------|--------|----------------|
| baby | Класс бэби | 4 | 6 | false |
| puppy | Класс щенков | 6 | 9 | false |
| junior | Класс юниоров | 9 | 18 | false |
| intermediate | Промежуточный класс | 15 | 24 | true |
| open | Открытый класс | 15 | NULL | true |
| working | Рабочий класс | 15 | NULL | true |
| champions | Класс чемпионов | 15 | NULL | true |
| veteran | Класс ветеранов | 96 | NULL | false |

### Как проверить

1. `python scripts/seed_references.py` — данные загружены
2. `GET /breeds?animal_type=dog` — список пород собак
3. `GET /breeds?animal_type=dog&group=1` — пастушьи породы
4. `POST /admin/breeds` без admin-токена — 403
5. `POST /admin/breeds` с admin-токеном — порода создана
6. `DELETE /admin/breeds/{id}` для породы с собаками — ошибка "нельзя удалить"
