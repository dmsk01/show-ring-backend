# Этап 5: Помёты и доска объявлений

### Цель

Реализовать публикацию помётов (привязка к питомнику и родителям) и универсальную доску объявлений с категориями, фильтрами и полнотекстовым поиском.

### Что появляется в проекте

- Помёты: заводчик публикует информацию о помёте (родители, дата, кол-во щенков, цена, статус)
- Доска объявлений: продажа щенков, услуги хендлера, вязка, груминг, другое
- Категории объявлений
- Фильтры: порода, город, цена, категория
- Полнотекстовый поиск PostgreSQL (tsvector)
- Галерея фото в объявлении (связь с files)

### Модель данных

Новые таблицы: `litters`, `classifieds`, `classified_images`

Ключевые связи:
- litter.kennel_id → kennels.id
- litter.father_id / mother_id → dogs.id
- classified.author_id → users.id
- classified.breed_id → breeds.id
- classified_images.file_id → files.id

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| POST | `/litters` | Объявить помёт | Breeder |
| GET | `/litters` | Список помётов (фильтры) | Public |
| GET | `/litters/{id}` | Подробности помёта | Public |
| PUT | `/litters/{id}` | Обновить (статус, кол-во) | Owner |
| POST | `/classifieds` | Создать объявление | Authenticated |
| GET | `/classifieds` | Список с фильтрами + поиск | Public |
| GET | `/classifieds/{id}` | Подробности | Public |
| PUT | `/classifieds/{id}` | Обновить | Owner |
| DELETE | `/classifieds/{id}` | Закрыть объявление | Owner |
| GET | `/classifieds/search?q=...` | Полнотекстовый поиск | Public |

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/models/litter.py` | Litter ORM |
| `app/models/classified.py` | Classified, ClassifiedImage ORM |
| `app/schemas/classified.py` | ClassifiedCreate, ClassifiedResponse, ClassifiedFilter |
| `app/schemas/litter.py` | LitterCreate, LitterResponse |
| `app/routers/litters.py` | CRUD помётов |
| `app/routers/classifieds.py` | CRUD объявлений + поиск |
| `app/services/classified.py` | Логика объявлений, модерация статусов |
| `app/repositories/classified.py` | SQL: фильтры, полнотекстовый поиск |
| `app/repositories/litter.py` | SQL-запросы помётов |

### Ключевые концепции

- **PostgreSQL Full-Text Search** — `tsvector`, `tsquery`, `to_tsvector('russian', ...)`, `ts_rank`
- **GIN-индекс** — для ускорения полнотекстового поиска
- **Составные фильтры** — SQLAlchemy Core для динамического построения WHERE
- **Статусная модель** — объявление: active → moderation → closed → archived
- **Счётчик просмотров** — `UPDATE classifieds SET views_count = views_count + 1`

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| Full-Text Search (Raw SQL) | `WHERE to_tsvector('russian', title \|\| ' ' \|\| description) @@ plainto_tsquery('russian', :query)` |
| GIN индекс | `CREATE INDEX idx_classifieds_fts ON classifieds USING GIN(to_tsvector('russian', title \|\| ' ' \|\| description))` |
| Динамические WHERE (Core) | Составление фильтров: `if breed_id: stmt = stmt.where(c.breed_id == breed_id)` |
| Сортировка + пагинация | ORDER BY + LIMIT/OFFSET, cursor-based pagination |
| Atomic UPDATE | Инкремент views_count без race condition |
| LEFT JOIN | Объявления с фото (может не быть фото) |

### Как проверить

1. `POST /litters` — публикация помёта с указанием родителей
2. `GET /litters?breed_id=1&status=available` — фильтрация
3. `POST /classifieds` — создать объявление с фото
4. `GET /classifieds?category=puppy_sale&city=Москва&breed_id=5` — фильтры
5. `GET /classifieds/search?q=немецкая овчарка щенок` — полнотекстовый поиск на русском
6. Повторный `GET /classifieds/{id}` — views_count увеличился
