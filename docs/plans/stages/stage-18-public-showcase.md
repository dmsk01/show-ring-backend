# Этап 18 — Публичная витрина: доработки доменов

## Цель

Доработать существующие домены (Dog, Classified, Kennel, Litter, Show) под
публичную витрину без логина: фото собак, серверная сортировка/фильтрация,
метка верификации, связи помёт↔щенки, счётчики. Витрина работает и без этого
(с заглушками), доработки убирают костыли и поднимают качество.

**Источник ТЗ:** `show-ring-frontend/docs/superpowers/specs/2026-06-03-showcase-backend-requirements.md`
(дата 2026-06-03). Приоритеты оттуда: P1 > P2 > P3.

> **Принцип проекта (см. этап 17):** бэкенд единообразен, фронт адаптируется.
> ТЗ уже в snake_case — конфликтов casing нет. Формы ответов держим как везде
> (пагинатор `*Page`, объект на detail).

## Что проверено по факту кода (важные уточнения к ТЗ)

1. **P1 фото:** модель `Dog` УЖЕ имеет `photos` (`DogPhoto`, таблица
   `dog_photos`, с `position`/`is_primary` — `app/models/dog.py:129`,
   `136`). Нет только: эндпоинта загрузки и вывода фото в `DogResponse`.
   → **Миграция под фото НЕ нужна.** Зеркалим готовый паттерн объявлений
   (`POST /classifieds/{id}/images`, `app/routers/classifieds.py:203`).
2. **P2 status:** публичный `GET /classifieds` УЖЕ жёстко отдаёт только
   `status=active` (`app/routers/classifieds.py:135`); closed/archived/
   moderation анониму не видны. → Клиентская фильтрация не нужна, пагинация
   активных корректна. Доработка сводится к подтверждению + опциональному
   `status`-параметру для авторских/админских выборок (не для витрины).
3. Сортировки (`sort_by/order`) нет ни в одном list-эндпоинте.
4. `KennelResponse` без `is_verified` (в модели `Kennel.is_verified` есть).
5. `Dog` без `litter_id` — связи помёт↔щенки сейчас нет вообще.

---

## P1 · Задача 1 — Фото собак (загрузка + вывод)

**1. Что делать.**
- Эндпоинт `POST /dogs/{id}/images` (auth: владелец питомника собаки/admin),
  зеркало `add_images` объявлений: принять список `file_id` (+опц.
  `position`/`is_primary`), создать строки `DogPhoto`.
- В `DogResponse` добавить `photo_file_ids: list[uuid.UUID]` (file_id всех
  фото, по `position`) и `avatar_file_id: uuid.UUID | None` (file_id фото с
  `is_primary`, иначе первое). Оба выводим из `DogPhoto` — без новой колонки.
- Репозиторий: грузить `Dog.photos` (`selectinload`) в detail и list.

**2. Как это работает.** `DogPhoto` уже моделирует галерею (m2m dog↔files,
`position`, `is_primary`). Файлы публичны (`GET /files/{id}`, `is_public=True`)
— фронт строит URL сам. Avatar — производное (is_primary), отдельная колонка
не нужна, и это не плодит второй источник правды (в отличие от
`Kennel.avatar_file_id`, где галереи нет).

**3. API / примеры.**
```python
# ответ
class DogResponse(DogBase):
    ...
    avatar_file_id: uuid.UUID | None = None
    photo_file_ids: list[uuid.UUID] = []

# сборка из ORM (photos подгружены selectinload, отсортированы по position)
photos = sorted(dog.photos, key=lambda p: p.position)
photo_file_ids = [p.file_id for p in photos]
avatar = next((p.file_id for p in photos if p.is_primary), None) or (photo_file_ids[0] if photo_file_ids else None)
```

**4. Зачем это нужно.** Главное P1: реальные фото в карточках/галерее вместо
placeholder — то, что «удешевляет» витрину питомников.

**5. Ключевые термины.**
- `DogPhoto` (`app/models/dog.py:136`) — галерея (position/is_primary).
- `selectinload(Dog.photos)` — загрузить фото без N+1 на списках.
- паттерн `POST /classifieds/{id}/images` — образец эндпоинта.

**6. Как проверить.** Загрузить файл (`POST /files/upload`), привязать
(`POST /dogs/{id}/images`), `GET /dogs/{id}` → `photo_file_ids` непустой,
`avatar_file_id` проставлен; `GET /files/{avatar_file_id}` отдаёт картинку.

---

## P2 · Задача 2 — Подтвердить/расширить фильтр статуса объявлений

**1. Что делать.** Подтвердить, что анонимный `GET /classifieds` отдаёт ТОЛЬКО
`active` (уже так — `classifieds.py:135`). Опционально: добавить query
`status: ClassifiedStatus | None` для авторских/админских выборок («мои
закрытые»), но публичный дефолт остаётся `active`. Зафиксировать тестом.

**2. Как это работает.** Публичный список форсит `status=active` на уровне
репозитория, поэтому пагинация активных уже корректна. Доп. параметр имеет
смысл только под авторизованного владельца — и тогда с проверкой прав.

**6. Как проверить.** Тест: создать active+archived объявления → анонимный
`GET /classifieds` возвращает только active; `total` считает только active.

---

## P2 · Задача 3 — Серверная сортировка (`sort_by` / `order`)

**1. Что делать.** Добавить в list-эндпоинты `sort_by: str` и
`order: Literal["asc","desc"] = "desc"` через **белый список** полей на домен:
- kennels: `name`, `created_at`;
- dogs: `name`, `date_of_birth`, `created_at`;
- classifieds: `created_at`, `price`, `views_count`;
- shows: `date_start`, `created_at`.
Применять в репозиториях через `order_by`.

**2. Как это работает.** Сортировка по неизвестному полю — это потенциальная
ошибка/инъекция, поэтому НЕ пробрасываем строку в `order_by` напрямую, а
маппим через словарь `{разрешённое_имя: колонка}`; не нашли — 422 или дефолт.
`asc()/desc()` от SQLAlchemy-колонки. Без сортировки клиент видит корректный
порядок только на одной странице — на многостраничных выборках он ломается.

**3. API / примеры.**
```python
_DOG_SORT = {"name": Dog.name, "date_of_birth": Dog.date_of_birth, "created_at": Dog.created_at}
col = _DOG_SORT.get(sort_by)
if col is None:
    raise HTTPException(422, "bad_sort_field")
stmt = stmt.order_by(col.asc() if order == "asc" else col.desc())
```

**4. Зачем это нужно.** Корректная сортировка витрин на всех страницах, а не
только на загруженной.

**5. Ключевые термины.**
- whitelist-маппинг `sort_by` → колонка (защита от инъекции).
- `Literal["asc","desc"]` — валидируемый Pydantic/FastAPI параметр.
- `column.asc()/.desc()` — направление сортировки SQLAlchemy.

**6. Как проверить.** `GET /dogs?sort_by=name&order=asc` — порядок по имени;
`?sort_by=hack` → 422.

---

## P2 · Задача 4 — `is_verified` в `KennelResponse`

**1. Что делать.** Добавить `is_verified: bool` в `KennelResponse`
(`app/schemas/kennel.py:53`). Колонка `Kennel.is_verified` уже существует —
только вывод.

**2. Как это работает.** `from_attributes=True` подтянет поле из ORM
автоматически, как только оно объявлено в схеме. Ставится модератором
(этап 12, `/admin/moderation/kennels/{id}/verify`).

**6. Как проверить.** `GET /kennels/{id}` → есть `is_verified: true/false`.

---

## P3 · Задача 5 — Связь помёт → щенки

**1. Что делать.** Добавить `litter_id: uuid.UUID | null` в модель `Dog`
(FK `litters.id` ON DELETE SET NULL, index) + миграция. Отразить в
`DogBase`/`DogResponse`. Дать выборку щенков помёта: фильтр
`GET /dogs?litter_id=` И/ИЛИ эндпоинт `GET /litters/{id}/puppies` →
`list[DogResponse]`.

**2. Как это работает.** Сейчас связи нет совсем (`Dog` знает только
father/mother). `litter_id` связывает конкретных собак с пометом → карточка
помёта показывает реальных щенков. SET NULL — удаление помёта не уносит собак.

**3. API / примеры.**
```python
litter_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("litters.id", ondelete="SET NULL"),
    nullable=True, index=True)
```

**4. Зачем это нужно.** Показ щенков помёта в объявлении, связка «объявление
о помёте ↔ конкретные собаки».

**5. Ключевые термины.**
- FK `ondelete="SET NULL"` — собака переживает удаление помёта.
- index на `litter_id` — под фильтр `?litter_id=`.

**6. Как проверить.** Привязать собак к помёту (`PUT /dogs/{id}` c `litter_id`),
`GET /litters/{id}/puppies` → список этих собак.

---

## P3 · Задача 6 — `father`/`mother` объектами в `LitterResponse`

**1. Что делать.** В `LitterResponse` добавить `father: DogRef | None` и
`mother: DogRef | None`, где `DogRef = {id, name, avatar_file_id}`. Резолвить
из `father_id`/`mother_id`. `father_id/mother_id` оставить (обратная
совместимость).

**2. Как это работает.** Сейчас фронт ради имени родителя делает доп. запрос
`/dogs/{id}`. Развёрнутый объект убирает round-trip. На СПИСКАХ помётов —
батч-загрузка родителей (`IN`/`selectinload`), чтобы не словить N+1 (как в
этапе документов).

**3. API / примеры.**
```python
class DogRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    avatar_file_id: uuid.UUID | None = None
```

**6. Как проверить.** `GET /litters/{id}` → `father.name` заполнен без
отдельного запроса к `/dogs`.

---

## P3 · Задача 7 — Счётчики у питомника

**1. Что делать.** Добавить в `KennelResponse` агрегаты `dogs_count: int`,
`litters_count: int` (+опц. `active_classifieds_count: int`). Считать
COUNT'ами; на СПИСКЕ питомников — одним сгруппированным запросом, не по N+1.

**2. Как это работает.** На карточке сетки нужны «N собак, M помётов» без
доп. запросов с фронта. Для detail — пара `SELECT count(*)`; для list —
`GROUP BY kennel_id` одним проходом и подстановка из словаря.

**6. Как проверить.** `GET /kennels` → у элементов `dogs_count/litters_count`
совпадают с реальным числом; на 50 питомниках — без всплеска числа запросов.

---

## P3 · Задача 8 — Тесты

`tests/integration/`:
- dog photos: upload → attach → `DogResponse.photo_file_ids/avatar_file_id`;
- classifieds: анонимный список только active;
- sort: `?sort_by=&order=` меняет порядок; неизвестное поле → 422;
- kennel: `is_verified` в ответе; счётчики совпадают;
- litter: `puppies` по `litter_id`; `father/mother` объектами.

---

## Что НЕ требует миграции / что требует

| Задача | Миграция |
|---|---|
| P1 фото собак | **нет** (таблица `dog_photos` уже есть) |
| P2 status / сортировка | нет |
| P2 `is_verified` в ответе | нет (колонка есть) |
| P3 `Dog.litter_id` | **да** (новая колонка + FK + index) |
| P3 father/mother объекты, счётчики | нет |

## Критерии готовности (для stage-verification)

- [ ] `POST /dogs/{id}/images` (auth владелец/admin); `DogResponse` отдаёт
      `photo_file_ids` + `avatar_file_id`.
- [ ] Подтверждено тестом: анонимный `GET /classifieds` — только active.
- [ ] `sort_by/order` (whitelist) на kennels/dogs/classifieds/shows;
      неизвестное поле → 422.
- [ ] `is_verified` в `KennelResponse`.
- [ ] `Dog.litter_id` + миграция; выборка щенков помёта работает.
- [ ] `father/mother` объектами в `LitterResponse` (без N+1 на списках).
- [ ] `dogs_count/litters_count` в `KennelResponse` (без N+1 на списках).
- [ ] Интеграционные тесты зелёные; `pytest -q` без регрессий.

## Связанные точки кода

- Паттерн images: `app/routers/classifieds.py:203` (`add_images`), сервис.
- Фото собак: `app/models/dog.py:136` (`DogPhoto`).
- Списки/пагинаторы: `DogPage`/`ClassifiedPage`/`KennelPage` + `list_*`/`count_*`.
- Статус объявлений: `app/routers/classifieds.py:135` (форс active).
- Верификация: `Kennel.is_verified` (`app/models/kennel.py`), этап 12 moderation.
- Анти-N+1 на агрегатах/родителях: подход из этапа документов (`document_official`).

## Технический долг / на потом

- `active_classifieds_count` у питомника, если понадобится на карточке.
- Кэш счётчиков питомника (Redis), если список питомников станет тяжёлым.
- Полноценная галерея с порядком/обложкой в UI (drag-sort `position`).
