# Этап 17 — Блог (Posts)

## Цель

Реализовать домен **Blog/Posts** под готовый фронт (Minimal Kit, `src/sections/blog/**`,
SWR `src/actions/blog.ts`), чтобы раздел заработал **без правок UI** — только
подключение данных. Контракт жёсткий: имена ключей-обёрток и поля `Post`
менять нельзя.

**Источник ТЗ:** `show-ring-frontend/docs/specs/2026-06-02-blog-backend-contract.md`
(дата 2026-06-02). Этот этап — его бэкенд-реализация.

## Контекст

- Фронт ходит на `/api/posts*`, Next-прокси форвардит на бэкенд (`:8000/posts*`).
- Раздел уже собран на компонентах; форма создания/редактирования
  (`PostCreateEditForm`) готова, нужен реальный сабмит.
- Обложки фронт грузит существующим `POST /files/upload` и кладёт готовый URL
  в `coverUrl` (наш `GET /files/{id}` уже отдаёт публичные картинки —
  `is_public=True` по умолчанию, этап review 2026-06-01).

## Ключевые решения и грабли (читать ДО кода)

> **Принцип проекта: противоречия трактуем в пользу бэкенда. Всё единообразно.**
> Бэкенд НЕ подстраивается под чужой контракт фронта — он сохраняет свои
> конвенции, а фронт адаптируется у себя (`src/actions/blog.ts` / Next-прокси).

1. **Никакого camelCase и обёрток — snake_case и формы как у всего API.**
   ТЗ фронта просит `coverUrl/totalViews/latestPosts` и обёртки
   `{posts}/{post}/{latestPosts}/{results}`. Мы это **не делаем**. Блог отдаёт:
   - **snake_case** поля (`cover_url`, `total_views`, `created_at`, `meta_title`…);
   - **списки** — пагинация `{items, total, page, per_page}` (как `DogPage`,
     `app/schemas/dog.py:95`);
   - **detail** — объект `PostResponse` напрямую (как `DogResponse`), без `{"post":…}`.

   Перекладку snake→camel, распаковку и переименование параметров делает ФРОНТ
   в своём adapter-слое (см. раздел «Что адаптирует фронт» ниже). Это правка на
   фронте, не на бэке.
2. **`content` = сырой HTML, и его съест глобальный middleware.**
   `app/middleware/sanitization.py` прогоняет ВСЕ строки тела через
   `bleach.clean(tags=[], strip=True)` → вырежет весь HTML из `content` ещё
   ДО хендлера. Нужно: (а) исключить `content` из глобальной санитизации
   (как `SENSITIVE_FIELDS`), (б) в сервисе поста санитизировать `content`
   СВОИМ allowlist-bleach (разрешить форматирование/заголовки/списки/ссылки/
   картинки, вырезать `<script>`, `on*`, `javascript:`). `bleach>=6.0.0` уже
   в зависимостях.
3. **Detail — по чистому пути `/posts/{slug}`** (как `/dogs/{id}`), не по
   `?title=<slug>`. ТЗ фронта тащит исторический `?title=` (значение = slug) —
   это как раз «противоречие в пользу бэкенда»: имя параметра, врущее про своё
   содержимое, не тянем. Фронт-адаптер маппит свой `?title=` на `/posts/{slug}`.
4. **Slug генерим сами** из title (kebab-case, уникальный, при коллизии суффикс
   `-2`). Для кириллицы нужна транслитерация (см. Задачу 3).
5. **v1-объём.** Comments/favorites — опционально. Для v1: `comments: []`,
   `favoritePerson: []`, `total*: 0` (счётчики — колонки с дефолтом 0).
   Реальные комментарии/избранное/инкремент просмотров — в техдолг этапа.
6. **Auth.** Read — публично (нужно для лендинга `/post`). Write — `Bearer` +
   роль `admin`/`organizer` (`require_any_role("admin","organizer")` из
   `app/dependencies.py`). 401 без токена, 403 при нехватке прав.

## Модель данных (эскиз `posts`)

| Колонка | Тип | Заметка |
|---|---|---|
| id | UUID PK | |
| title | String(300) | |
| slug | String(320) UNIQUE, index | lookup идёт по нему |
| description | Text | |
| content | Text | санитизированный HTML |
| cover_url | String(1024) null | готовый URL от фронта |
| tags | ARRAY(String) | server_default '{}' |
| meta_keywords | ARRAY(String) | server_default '{}' |
| meta_title / meta_description | String/Text null | |
| publish | Enum(published, draft) | default draft |
| author_id | UUID FK users SET NULL | автор |
| total_views/shares/comments/favorites | Integer | default 0 |
| created_at / updated_at | timestamptz | TimestampMixin |

> `tags`/`meta_keywords` как PG `ARRAY(String)` — проще, чем join-таблица, и
> хватает под контракт (поиск по тегам = `tags && ARRAY[:q]` или `ANY`).

---

## Задача 1 — Модель `Post` + миграция

**1. Что делать.** `app/models/post.py`: класс `Post(Base, TimestampMixin)` по
эскизу выше + enum `PostPublish(published/draft)`. Зарегистрировать импорт в
`app/models/__init__.py` и `migrations/env.py` (как другие модели). Миграция
(down_revision = текущий head) создаёт таблицу + UNIQUE/index на `slug`.

**2. Как это работает.** `ARRAY(String)` (из `sqlalchemy.dialects.postgresql`)
хранит теги одной колонкой-массивом. `slug` UNIQUE + index — это «естественный
ключ» для публичных URL и lookup'а. Enum `publish` как PG-тип (`SAEnum(..., name=...)`).

**3. API / примеры.**
```python
from sqlalchemy.dialects.postgresql import ARRAY, UUID
class Post(Base, TimestampMixin):
    __tablename__ = "posts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), server_default="{}")
    publish: Mapped[PostPublish] = mapped_column(SAEnum(PostPublish, name="postpublish"), default=PostPublish.draft, index=True)
    total_views: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ...
```

**4. Зачем это нужно.** Хранилище постов. Отдельная таблица, индекс по slug —
быстрый публичный lookup без сканов.

**5. Ключевые термины.**
- `ARRAY(String)` — PG-массив строк (теги/ключевые слова).
- `SAEnum(..., name="postpublish")` — PG enum-тип для статуса.
- `TimestampMixin` — created_at/updated_at (как у Show/Dog).

**6. Как проверить.** `alembic upgrade head`; `\d posts` в psql показывает
колонки, UNIQUE на slug, тип `postpublish`.

---

## Задача 2 — Pydantic-схемы (snake_case, как везде)

**1. Что делать.** `app/schemas/post.py` — обычные `BaseModel` с
`from_attributes=True`, **snake_case**, без alias-генераторов и обёрток:
- `Author` (`name`, `avatar_url`);
- `PostCard` (карточка списка: `id, title, slug, description, cover_url,
  created_at, author, total_views, total_shares, total_comments,
  total_favorites, tags, publish`);
- `PostResponse` (полный: + `content`, `meta_*`, `comments`, `favorite_person`);
- `PostCreate` / `PostUpdate` (вход — snake_case);
- `PostPage` (`items: list[PostCard]`, `total`, `page`, `per_page`) — как `DogPage`.

**2. Как это работает.** Никакого camelCase: бэкенд единообразен с остальным
API (`DogResponse`, `NotificationResponse` — все snake_case). Список —
пагинированная обёртка `PostPage` ровно той же формы, что `DogPage`/
`ClassifiedPage`. detail-эндпоинт возвращает `PostResponse` напрямую, без
`{"post": …}`. Перевод в camel и обёртки фронта — на фронте.

**3. API / примеры.**
```python
from pydantic import BaseModel, ConfigDict

class Author(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    avatar_url: str = ""

class PostPage(BaseModel):          # один-в-один форма DogPage
    items: list[PostCard]
    total: int
    page: int
    per_page: int
```

**4. Зачем это нужно.** Единообразие: один стиль ответов на весь проект →
меньше сюрпризов, общий пагинатор, общий тон. Контракт фронта закрывается его
adapter-слоем, а не «особенным» бэкендом.

**5. Ключевые термины.**
- `DogPage` (`app/schemas/dog.py:95`) — эталон пагинированного ответа.
- `from_attributes=True` — маппинг из ORM (как у всех Response-схем).

**6. Как проверить.** Юнит: `PostPage(...).model_dump()` содержит
`items/total/page/per_page`; ключи полей — snake_case (`cover_url`,
`total_views`, `created_at`).

---

## Задача 3 — Генерация slug (kebab + транслит + уникальность)

**1. Что делать.** Утилита `app/utils/slug.py: make_slug(title, exists) -> str`.
kebab-case, транслитерация кириллицы в латиницу, при коллизии — суффикс `-2`,
`-3`, … `exists` — колбэк/функция проверки занятости slug в БД.

**2. Как это работает.** Голый kebab от кириллицы даст пустую/мусорную строку,
поэтому нужна транслитерация. Варианты: подключить `python-slugify` (надёжно
обрабатывает unicode) или маленькая своя translit-карта. Уникальность — цикл с
суффиксом, проверяя БД (UNIQUE на slug всё равно страхует от гонок).

**3. API / примеры.**
```python
# с python-slugify:
from slugify import slugify
base = slugify("Новости выставки!")  # -> "novosti-vystavki"
slug, i = base, 2
while await post_repo.slug_exists(db, slug):
    slug = f"{base}-{i}"; i += 1
```

**4. Зачем это нужно.** slug — публичный URL и ключ lookup'а. Нечитаемые/
неуникальные slug ломают и SEO, и адресацию detail/edit.

**5. Ключевые термины.**
- `slugify()` — unicode → ascii kebab.
- UNIQUE-constraint на slug — защита от гонок при параллельном создании.

**6. Как проверить.** Юнит: `make_slug` на кириллице даёт латиницу; два поста с
одинаковым title → `foo` и `foo-2`.

---

## Задача 4 — Санитизация HTML-контента (+ обход глобального middleware)

**1. Что делать.**
- В `app/middleware/sanitization.py` добавить `content` в passthrough (рядом с
  `SENSITIVE_FIELDS`) — иначе глобальный bleach вырежет весь HTML.
- Утилита `app/utils/html_sanitize.py: sanitize_post_html(html) -> str` с
  allowlist (bleach): теги `p,h1..h4,strong,em,u,a,ul,ol,li,blockquote,code,pre,img,br,span`,
  атрибуты `a[href,title], img[src,alt]`, протоколы `http/https/relative`,
  strip `<script>`, `on*`, `javascript:`.
- Применять в сервисе при `POST`/`PUT` к `content`.

**2. Как это работает.** `bleach.clean(html, tags=ALLOWED, attributes=ATTRS,
protocols=PROTO, strip=True)` оставляет безопасное форматирование и режет
опасное. Это пользовательский HTML из WYSIWYG → классический XSS-вектор,
санитизация ОБЯЗАТЕЛЬНА на записи. Картинки `/api/files/{id}` (relative) не
режем.

**3. API / примеры.**
```python
import bleach
ALLOWED = ["p","h1","h2","h3","h4","strong","em","u","a","ul","ol","li",
           "blockquote","code","pre","img","br","span"]
ATTRS = {"a": ["href","title","target","rel"], "img": ["src","alt"]}
def sanitize_post_html(html: str) -> str:
    return bleach.clean(html, tags=ALLOWED, attributes=ATTRS,
                        protocols=["http","https"], strip=True)
```

**4. Зачем это нужно.** Без allowlist либо XSS (если хранить raw), либо пустой
контент (если глобальный middleware его вырежет). Оба варианта ломают блог.

**5. Ключевые термины.**
- `bleach.clean(tags, attributes, protocols, strip)` — allowlist-санитайзер.
- `SanitizationMiddleware` passthrough — поля, которые НЕ трогаем глобально.

**6. Как проверить.** Юнит: `sanitize_post_html("<p>ok</p><script>x</script>")`
→ `<p>ok</p>`; `<img src="/api/files/1">` сохраняется; `<a href="javascript:…">`
теряет href.

---

## Задача 5 — Репозиторий + read-эндпоинты (публичные)

**1. Что делать.** `app/repositories/post.py` (CRUD + `list_page`, `get_by_slug`,
`related`, `search`, `slug_exists`) и роутер `app/routers/posts.py` —
RESTful и единообразно:

| Метод/путь | Query | Ответ |
|---|---|---|
| `GET /posts` | `?publish=&query=&page=&per_page=` | `PostPage` (`{items,total,page,per_page}`) |
| `GET /posts/{slug}` | — | `PostResponse` · 404 |
| `GET /posts/{slug}/related` | `?limit=` | `list[PostCard]` (последние, **без** этого slug) |

Поиск свёрнут в `GET /posts?query=` (а не отдельный `/search`); «latest» —
`/posts/{slug}/related`. Это убирает несколько разнородных ручек в пользу
одной конвенции.

**2. Как это работает.** detail — по чистому пути `/posts/{slug}`. Фильтр
`query` — `ILIKE` по title/description + пересечение по тегам
(`Post.tags.op("&&")(cast([q], ARRAY(String)))` или `:q = ANY(tags)`). Список
— пагинатор как у Dogs. Порядок роутов: `/posts/{slug}` параметрический,
коллизий с `GET /posts` (без сегмента) и `POST /posts` (другой метод) нет.

**3. API / примеры.**
```python
@router.get("/posts/{slug}", response_model=PostResponse)
async def get_post(slug: str, db: AsyncSession = Depends(get_db)):
    post = await post_repo.get_by_slug(db, slug)
    if post is None:
        raise HTTPException(404, "not_found")
    return await _to_response(db, post)   # объект напрямую, без обёртки
```

**4. Зачем это нужно.** Публичная витрина блога (лендинг, SEO). Read без
авторизации. Формы — те же, что в остальном API, чтобы не плодить исключения.

**5. Ключевые термины.**
- `ILIKE` — регистронезависимый поиск (PG).
- `ARRAY &&` / `ANY(array)` — поиск по тегам.
- `PostPage` — пагинатор как `DogPage`.

**6. Как проверить.** `GET /posts` → `{"items":[…],"total":…}`;
`GET /posts/<slug>` → объект `PostResponse`; неизвестный slug → 404.

---

## Задача 6 — Write-эндпоинты (auth admin/organizer)

**1. Что делать.** В `app/routers/posts.py`:

| Метод/путь | Ответ | |
|---|---|---|
| `POST /posts` | `PostResponse` 201 | slug генерим, content санитизируем |
| `PUT /posts/{id}` | `PostResponse` 200 | полное/частичное обновление |
| `DELETE /posts/{id}` | 204 | 404 если нет |

Зависимость `Depends(require_any_role("admin","organizer"))`. `publish`
принимаем в теле (`published`/`draft`). Ответы — объект `PostResponse`
напрямую (без `{"post":…}`), как `DogResponse`.

**2. Как это работает.** Сервис: валидирует (content ≥ 100 символов, tags ≥ 2,
meta_keywords ≥ 1 — как в форме), санитизирует `content` (Задача 4), генерит
slug (Задача 3) при создании или при смене title, ставит `author_id =
current_user.id`, сохраняет, возвращает `PostResponse` (объект напрямую).

**3. API / примеры.**
```python
@router.post("/posts", response_model=PostResponse, status_code=201,
             dependencies=[Depends(require_any_role("admin","organizer"))])
async def create_post(body: PostCreate, db=Depends(get_db),
                      user: User = Depends(get_current_user)):
    post = await post_service.create(db, body, author=user)
    return await _to_response(db, post)   # объект напрямую
```

**4. Зачем это нужно.** Редактор постов для админов/организаторов. Санитизация
и slug — на бэке, фронт им не доверяет.

**5. Ключевые термины.**
- `require_any_role(*roles)` — фабрика dependency проверки ролей.
- `get_current_user` — текущий пользователь (автор).

**6. Как проверить.** `POST /posts` с токеном админа → 201 + объект
`PostResponse` со slug; без токена → 401; токен `buyer` → 403; `<script>` в
content вырезан.

---

## Задача 7 — Сборка `Post` (author, counters, v1-пустышки)

**1. Что делать.** Хелпер `_to_response`/`_to_card(db, post)`: собрать `author`
(`{name, avatar_url}` из `User`+profile через `full_name`, avatar — `/files/{id}`
по `avatar_file_id`), проставить `comments: []`, `favorite_person: []`,
`total_*` из колонок (0 по умолчанию). Никогда не отдавать `null` в этих полях
(пустой объект/массив вместо null).

**2. Как это работает.** Непустые объекты/массивы — чтобы любой клиент не падал
на `null`. author резолвится из автора поста (один `db.get(User)` + профиль
selectinload — батчить при списках, чтобы не словить N+1, как в этапе документов).

**3. API / примеры.**
```python
author = Author(name=full_name(author_user) or author_user.email,
                avatar_url=f"/files/{author_user.avatar_file_id}" if author_user.avatar_file_id else "")
```

**4. Зачем это нужно.** `null` в `author/comments/tags` ломает клиентов. Единый
сборщик гарантирует форму ответа (snake_case, без null в коллекциях).

**5. Ключевые термины.**
- `full_name(user)` — ФИО из профиля (`app/utils/names.py`).
- `selectinload` — батч-загрузка авторов для списков (анти-N+1).

**6. Как проверить.** В ответе `author.name` непустой, `comments == []`,
`tags` — массив; `cover_url` строкой; все ключи snake_case.

---

## Задача 8 — Тесты (integration)

`tests/integration/test_blog.py`:
- public read: `GET /posts` (форма `PostPage`), `GET /posts/{slug}` (объект),
  `GET /posts/{slug}/related`; ключи snake_case;
- auth write: `POST /posts` под admin → 201; без токена → 401; роль buyer → 403;
- slug: два поста с одним title → `slug` и `slug-2`;
- XSS: `content` с `<script>` сохраняется без него; `<p>`/`<img>` остаются;
- (если делаем) comments.

Использовать существующий харнесс (`tests/integration/conftest.py`).

---

## Контракт эндпоинтов (как делает БЭКЕНД, уже не как ТЗ фронта)

- `GET /posts?publish=&query=&page=&per_page=` → `PostPage`
  (`{items,total,page,per_page}`).
- `GET /posts/{slug}` → `PostResponse` (объект) / 404.
- `GET /posts/{slug}/related?limit=` → `list[PostCard]`.
- `POST /posts` → `PostResponse` (201); `PUT /posts/{id}` → `PostResponse`;
  `DELETE /posts/{id}` → 204.
- Опц.: `POST /posts/{id}/comments`, `…/replies`; опц.:
  `PATCH /posts/{id}/publish {"publish": "..."}`.

Всё snake_case, формы — как у Dogs/Notifications. Никаких
`{posts}/{post}/{latestPosts}/{results}` и camelCase.

## Что адаптирует ФРОНТ (отдельная задача на фронте)

Поскольку бэкенд единообразен, маппинг под Minimal Kit делает фронт в
`src/actions/blog.ts` (и/или Next-прокси):

| Фронт ждёт | Бэкенд даёт | Маппинг на фронте |
|---|---|---|
| `{posts: Post[]}` | `PostPage` | `data.items` (+ при желании total/page) |
| `{post: Post}` | `PostResponse` (объект) | завернуть в `{post: data}` |
| `{latestPosts}` `?title=slug` | `GET /posts/{slug}/related` | завернуть в `{latestPosts: data}` |
| `{results}` `?query=` | `GET /posts?query=` | `{results: data.items}` |
| camelCase (`coverUrl`…) | snake_case (`cover_url`…) | camelize в адаптере |

> Это сознательное решение: один backend-стиль на весь проект, перекладка —
> дешёвая и локальная на фронте. Фронту передать эту таблицу.

## Критерии готовности (для stage-verification)

- [ ] Таблица `posts` + миграция; slug UNIQUE+index.
- [ ] Ответы **snake_case** и в формах проекта: списки — `PostPage`
      (`items/total/page/per_page`), detail — объект напрямую. Никакого camel/обёрток.
- [ ] Read-эндпоинты публичные; detail — `GET /posts/{slug}`.
- [ ] Write-эндпоинты под `admin`/`organizer`; 401/403 корректны.
- [ ] slug генерируется (транслит кириллицы) и уникален (`-2`).
- [ ] `content` санитизируется allowlist'ом; глобальный middleware его НЕ режет.
- [ ] `author/comments/favorite_person/tags/meta_keywords` никогда не `null`.
- [ ] Интеграционные тесты зелёные; `pytest -q` без регрессий.

## Связанные точки кода

- Санитизация: `app/middleware/sanitization.py` (passthrough), `bleach` (requirements).
- Роли: `app/dependencies.py` (`require_any_role`, `get_current_user`).
- Файлы/обложки: `POST /files/upload`, `GET /files/{id}` (`is_public=True`).
- Автор: `app/utils/names.py` (`full_name`), `User.avatar_file_id`.
- Пагинатор-эталон: `app/schemas/dog.py:95` (`DogPage`) — повторяем форму.

## Технический долг / на потом

- Реальные комментарии (`post_comments` + replies) и избранное
  (`post_favorites` + `favoritePerson`, `totalFavorites`).
- Инкремент `totalViews` на detail (атомарный UPDATE, как у ad-views).
- Пагинация `GET /posts` (`{items,total,page,per_page}` как в Dogs), когда
  постов станет много.
- `PATCH /posts/{id}/publish` отдельным эндпоинтом (если фронт попросит).
- Возможный перенос тегов в join-таблицу, если понадобится агрегация по тегам.
