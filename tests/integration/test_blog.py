"""
Интеграция: блог (этап 17).

Покрывает:
- публичный read: список (форма PostPage, snake_case), detail по slug
  (объект), related (последние, без текущего), 404 на неизвестный slug;
- auth write: POST под admin → 201; без токена → 401; без роли → 403;
- генерацию slug (транслит кириллицы + суффикс -2 при коллизии);
- санитизацию content (XSS): <script> вырезан, <p>/<img> сохранены.

Данные вставляем напрямую через db_session (как в остальных интеграционных
тестах), автора-писателя регистрируем через API ради рабочего JWT и выдаём
ему роль admin строкой в user_roles.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.post import Post, PostPublish
from app.models.user import RoleEnum, User, UserRole

PASSWORD = "secret123"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_user(client) -> tuple[uuid.UUID, str]:
    """Регистрирует и логинит пользователя, возвращает (id, access_token)."""
    email = f"itest_{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    r = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    access = r.json()["access_token"]
    me = await client.get("/users/me", headers=_auth(access))
    return uuid.UUID(me.json()["id"]), access


async def _make_admin(client, db_session) -> tuple[uuid.UUID, str]:
    uid, token = await _make_user(client)
    db_session.add(UserRole(user_id=uid, role=RoleEnum.admin))
    await db_session.commit()
    return uid, token


async def _make_writer(
    client, db_session, role: RoleEnum = RoleEnum.organizer
) -> tuple[uuid.UUID, str]:
    """Пользователь с writer-ролью (по умолчанию organizer) — может писать
    посты, но (не admin) только свои."""
    uid, token = await _make_user(client)
    db_session.add(UserRole(user_id=uid, role=role))
    await db_session.commit()
    return uid, token


async def _author(db_session) -> User:
    u = User(
        email=f"author_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x"
    )
    db_session.add(u)
    await db_session.commit()
    return u


async def _post(
    db_session,
    author: User,
    *,
    title: str = "Заголовок поста",
    slug: str | None = None,
    publish: PostPublish = PostPublish.published,
    content: str = "<p>текст</p>",
) -> Post:
    p = Post(
        title=title,
        slug=slug or f"slug-{uuid.uuid4().hex[:8]}",
        description="описание",
        content=content,
        publish=publish,
        author_id=author.id,
    )
    db_session.add(p)
    await db_session.commit()
    return p


# ---------------------------------------------------------------------
# public read
# ---------------------------------------------------------------------


async def test_list_shape_is_postpage_snake_case(client, db_session):
    author = await _author(db_session)
    await _post(db_session, author)

    r = await client.get("/posts")
    assert r.status_code == 200, r.text
    body = r.json()
    # Форма PostPage, не обёртка {posts: ...}.
    assert set(body.keys()) == {"items", "total", "page", "per_page"}
    assert body["total"] >= 1
    card = body["items"][0]
    # Ключи snake_case (не camelCase).
    for key in ("cover_url", "total_views", "created_at", "author"):
        assert key in card
    assert "coverUrl" not in card
    # author — непустой объект, не null.
    assert card["author"]["name"]


async def test_detail_returns_object_by_slug(client, db_session):
    author = await _author(db_session)
    post = await _post(db_session, author, content="<p>привет</p>")

    r = await client.get(f"/posts/{post.slug}")
    assert r.status_code == 200, r.text
    body = r.json()
    # Объект напрямую, без {"post": ...}.
    assert body["slug"] == post.slug
    assert body["content"] == "<p>привет</p>"
    # Коллекции-пустышки никогда не null.
    assert body["comments"] == []
    assert body["favorite_person"] == []
    assert isinstance(body["tags"], list)


async def test_detail_unknown_slug_404(client):
    r = await client.get(f"/posts/{uuid.uuid4().hex}")
    assert r.status_code == 404


async def test_related_excludes_self(client, db_session):
    author = await _author(db_session)
    base = await _post(db_session, author, slug="base-post")
    await _post(db_session, author, slug="other-post")

    r = await client.get(f"/posts/{base.slug}/related")
    assert r.status_code == 200, r.text
    slugs = [p["slug"] for p in r.json()]
    assert base.slug not in slugs


# ---------------------------------------------------------------------
# auth write
# ---------------------------------------------------------------------


async def test_create_without_token_401(client):
    r = await client.post("/posts", json={"title": "X", "content": "<p>y</p>"})
    assert r.status_code == 401


async def test_create_without_role_403(client):
    _uid, token = await _make_user(client)
    r = await client.post(
        "/posts",
        json={"title": "X", "content": "<p>y</p>"},
        headers=_auth(token),
    )
    assert r.status_code == 403


async def test_create_as_admin_201_with_slug(client, db_session):
    _uid, token = await _make_admin(client, db_session)
    r = await client.post(
        "/posts",
        json={"title": "Новости выставки", "content": "<p>контент</p>"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # slug сгенерён транслитом кириллицы.
    assert body["slug"] == "novosti-vystavki"
    assert body["author"]["name"]


async def test_slug_collision_suffix(client, db_session):
    _uid, token = await _make_admin(client, db_session)
    payload = {"title": "Одинаковый заголовок", "content": "<p>a</p>"}
    r1 = await client.post("/posts", json=payload, headers=_auth(token))
    r2 = await client.post("/posts", json=payload, headers=_auth(token))
    assert r1.status_code == 201 and r2.status_code == 201
    base = r1.json()["slug"]
    assert r2.json()["slug"] == f"{base}-2"


async def test_content_sanitized_on_create(client, db_session):
    _uid, token = await _make_admin(client, db_session)
    raw = '<p>ok</p><script>alert(1)</script><img src="/files/1" alt="x">'
    r = await client.post(
        "/posts",
        json={"title": "XSS тест", "content": raw},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    content = r.json()["content"]
    assert "<script>" not in content
    assert "alert(1)" not in content
    assert "<p>ok</p>" in content
    # Относительная картинка сохраняется.
    assert '<img src="/files/1"' in content


# ---------------------------------------------------------------------
# видимость черновиков (аудит H1)
# ---------------------------------------------------------------------


async def test_draft_hidden_from_anonymous_list(client, db_session):
    author = await _author(db_session)
    await _post(db_session, author, slug="pub-1", publish=PostPublish.published)
    await _post(db_session, author, slug="draft-1", publish=PostPublish.draft)

    r = await client.get("/posts")
    assert r.status_code == 200, r.text
    slugs = [c["slug"] for c in r.json()["items"]]
    assert "pub-1" in slugs
    assert "draft-1" not in slugs  # черновик не виден анониму


async def test_draft_detail_404_for_anonymous(client, db_session):
    author = await _author(db_session)
    post = await _post(db_session, author, slug="draft-2", publish=PostPublish.draft)

    r = await client.get(f"/posts/{post.slug}")
    assert r.status_code == 404  # черновик по slug анониму недоступен


async def test_draft_visible_to_admin(client, db_session):
    _uid, token = await _make_admin(client, db_session)
    author = await _author(db_session)
    post = await _post(db_session, author, slug="draft-3", publish=PostPublish.draft)

    # Админ видит черновик и в detail, и в списке с ?publish=draft.
    r = await client.get(f"/posts/{post.slug}", headers=_auth(token))
    assert r.status_code == 200, r.text
    r = await client.get("/posts?publish=draft", headers=_auth(token))
    slugs = [c["slug"] for c in r.json()["items"]]
    assert "draft-3" in slugs


# ---------------------------------------------------------------------
# rate-limit публичного списка/поиска (аудит M1)
# ---------------------------------------------------------------------


async def test_posts_list_under_rate_limit_ok(client, db_session):
    author = await _author(db_session)
    await _post(db_session, author, slug="rl-1", publish=PostPublish.published)
    # Несколько запросов подряд в пределах лимита — все 200 (не 429).
    for _ in range(5):
        r = await client.get("/posts?query=rl")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------
# владение постом при правке/удалении (аудит L1)
# ---------------------------------------------------------------------


async def test_post_edit_requires_ownership(client, db_session):
    _a_id, a_tok = await _make_writer(client, db_session)
    _b_id, b_tok = await _make_writer(client, db_session)
    r = await client.post(
        "/posts", json={"title": "Чужой пост", "content": "<p>x</p>"},
        headers=_auth(a_tok),
    )
    assert r.status_code == 201, r.text
    post_id = r.json()["id"]

    # B (organizer, не автор) не может править/удалять чужой пост.
    r = await client.put(
        f"/posts/{post_id}", json={"title": "Взлом"}, headers=_auth(b_tok)
    )
    assert r.status_code == 403, r.text
    r = await client.delete(f"/posts/{post_id}", headers=_auth(b_tok))
    assert r.status_code == 403, r.text

    # A (автор) — может.
    r = await client.put(
        f"/posts/{post_id}", json={"title": "Правка автора"},
        headers=_auth(a_tok),
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Правка автора"


async def test_admin_can_edit_any_post(client, db_session):
    _a_id, a_tok = await _make_writer(client, db_session)
    _admin_id, admin_tok = await _make_admin(client, db_session)
    r = await client.post(
        "/posts", json={"title": "Под админом", "content": "<p>x</p>"},
        headers=_auth(a_tok),
    )
    post_id = r.json()["id"]

    # Админ может удалить любой пост.
    r = await client.delete(f"/posts/{post_id}", headers=_auth(admin_tok))
    assert r.status_code == 204, r.text
