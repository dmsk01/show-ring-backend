# Этап 4: Питомники и собаки

### Цель

Реализовать профили питомников, карточки собак с родословными и загрузку файлов (фото, документы).

### Что появляется в проекте

- Страница питомника: название, заводская приставка, описание, контакты, список собак
- Карточка собаки: кличка, порода, родословная (отец/мать), фото, документы
- Загрузка файлов (фото собак, аватар питомника, сканы документов)
- Файловое хранилище через **MinIO** (S3-совместимый, работает одинаково в dev и prod)
- Валидация файлов: проверка **magic bytes** (сигнатура файла), не только MIME-заголовок
- Поиск собак с фильтрами (порода, пол, город)

### Модель данных

Новые таблицы: `kennels`, `dogs`, `files`

Ключевые связи:
- kennel.owner_id → users.id (владелец питомника)
- dog.kennel_id → kennels.id (собака принадлежит питомнику)
- dog.breed_id → breeds.id
- dog.father_id → dogs.id (самореференс — родословная)
- dog.mother_id → dogs.id
- dog_photos (dog_id → dogs, file_id → files) — фото собак (many-to-many)
- users.avatar_file_id → files (аватар, прямой FK)

> **Связи с файлами — через FK на стороне владельца** (не полиморфные). Это гарантирует целостность на уровне БД.

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| POST | `/kennels` | Создать питомник | Breeder |
| GET | `/kennels` | Список питомников (фильтры) | Public |
| GET | `/kennels/{id}` | Страница питомника + собаки | Public |
| PUT | `/kennels/{id}` | Обновить питомник | Owner |
| POST | `/dogs` | Добавить собаку | Breeder |
| GET | `/dogs` | Поиск собак (фильтры) | Public |
| GET | `/dogs/{id}` | Карточка собаки + родословная | Public |
| PUT | `/dogs/{id}` | Обновить собаку | Owner |
| GET | `/dogs/{id}/pedigree` | Дерево родословной (3-4 поколения) | Public |
| POST | `/files/upload` | Загрузить файл | Authenticated |
| GET | `/files/{id}` | Скачать / показать файл | Public |

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/models/kennel.py` | Kennel ORM |
| `app/models/dog.py` | Dog ORM (с self-referential FK для родословной) |
| `app/models/file.py` | UploadedFile ORM |
| `app/schemas/kennel.py` | KennelCreate, KennelResponse |
| `app/schemas/dog.py` | DogCreate, DogResponse, PedigreeResponse |
| `app/schemas/file.py` | FileResponse |
| `app/routers/kennels.py` | CRUD питомников |
| `app/routers/dogs.py` | CRUD собак |
| `app/routers/files.py` | Upload / download |
| `app/services/kennel.py` | Бизнес-логика питомников |
| `app/services/dog.py` | Бизнес-логика собак, построение родословной |
| `app/services/file_storage.py` | Загрузка/скачивание файлов через MinIO (S3), валидация magic bytes |
| `app/repositories/kennel.py` | SQL-запросы питомников |
| `app/repositories/dog.py` | SQL-запросы собак |

### Ключевые концепции

- **Self-referential FK** — dog.father_id → dogs.id для дерева родословной
- **Рекурсивные запросы** — построение родословной на 3-4 поколения
- **File upload** — FastAPI `UploadFile`, валидация magic bytes + MIME, ограничение размера
- **MinIO (S3)** — `boto3` / `aioboto3` для загрузки/скачивания файлов
- **Magic bytes** — проверка первых байтов файла (JPEG: `FF D8 FF`, PNG: `89 50 4E 47`, PDF: `25 50 44 46`)
- **Eager/Lazy loading** — когда загружать связанные объекты

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| Self-referential FK | dog.father_id, dog.mother_id → dogs.id |
| Рекурсивный CTE (Raw SQL) | `WITH RECURSIVE pedigree AS (...)` для родословной |
| JOIN нескольких таблиц | dogs + breeds + kennels + files |
| Динамические фильтры (Core) | Поиск собак: порода AND/OR пол AND/OR город |
| Подзапросы | Количество собак в питомнике |

### Как проверить

1. `POST /kennels` — создать питомник
2. `POST /dogs` — добавить собаку с указанием отца/матери
3. `POST /files/upload` — загрузить фото, получить file_id
4. `GET /dogs/{id}/pedigree` — дерево родословной (3 поколения)
5. `GET /dogs?breed_id=1&sex=male&city=Москва` — поиск с фильтрами
6. `GET /kennels/{id}` — страница питомника со списком собак
