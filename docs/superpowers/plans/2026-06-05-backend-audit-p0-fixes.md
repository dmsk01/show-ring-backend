# Backend Audit P0 Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three P0 findings from the 2026-06-05 backend audit — forgeable JWT via weak `SECRET_KEY` (C1), publicly visible blog drafts (H1), and unprotected public blog read/search (M1).

**Architecture:** C1 adds a fail-fast `model_validator` to `Settings`. H1 adds an optional-auth dependency so blog read endpoints can hide drafts from non-writers. M1 adds the existing `check_rate_limit` pattern to the public blog list.

**Tech Stack:** FastAPI, Pydantic v2 / pydantic-settings, SQLAlchemy async, pytest + httpx integration harness, Redis sliding-window rate limit.

Spec: `docs/superpowers/specs/2026-06-05-backend-audit-fix-plan.md`.

**Test env note:** integration/unit runs need a STRONG `SECRET_KEY` (≥32 chars), e.g.
`$env:SECRET_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"` — the C1 validator now rejects weak keys when `debug=False` (which is the default in tests).

---

## Task 1: C1 — fail-fast on weak `SECRET_KEY`

**Files:**
- Modify: `app/config.py` (add `model_validator`)
- Test: `tests/unit/test_config_validation.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_config_validation.py`:

```python
"""Unit: Settings отвергает небезопасный SECRET_KEY (аудит C1)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

_DB = "postgresql+asyncpg://u:p@localhost:5432/db"
_STRONG = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def test_empty_secret_rejected_in_prod():
    with pytest.raises(ValidationError):
        Settings(database_url=_DB, secret_key="", debug=False)


def test_placeholder_secret_rejected_in_prod():
    with pytest.raises(ValidationError):
        Settings(database_url=_DB, secret_key="change-me-in-production", debug=False)


def test_short_secret_rejected_in_prod():
    with pytest.raises(ValidationError):
        Settings(database_url=_DB, secret_key="too-short", debug=False)


def test_strong_secret_accepted_in_prod():
    s = Settings(database_url=_DB, secret_key=_STRONG, debug=False)
    assert s.secret_key == _STRONG


def test_weak_secret_allowed_in_debug():
    # В dev допускаем (громкий warning), чтобы локальный стек не падал.
    s = Settings(database_url=_DB, secret_key="", debug=True)
    assert s.debug is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/unit/test_config_validation.py -q`
Expected: FAIL (no validator yet — weak keys are accepted, `raises` not triggered).

- [ ] **Step 3: Implement the validator**

In `app/config.py`, add imports at top:

```python
import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
```

Add inside `class Settings`, after the fields (before the closing of the class / before `settings = Settings()`):

```python
    # Аудит C1: SECRET_KEY подписывает HS256 access-JWT (app/utils/security.py).
    # Пустой/плейсхолдерный/короткий ключ делает токены подделываемыми
    # (обход авторизации). В prod (debug=False) — падаем на старте; в dev
    # (debug=True) — громкий warning, но поднимаемся, чтобы не ломать
    # локальный стек. model_validator (а не field_validator), т.к. нужен
    # доступ к debug, объявленному после secret_key.
    _SECRET_PLACEHOLDERS = frozenset(
        {"", "change-me-in-production", "change-me", "changeme", "secret"}
    )

    @model_validator(mode="after")
    def _validate_secret_key(self) -> "Settings":
        key = self.secret_key.strip()
        if key in Settings._SECRET_PLACEHOLDERS:
            reason = "пустой или плейсхолдер"
        elif len(key) < 32:
            reason = "короче 32 символов"
        else:
            return self
        msg = (
            f"SECRET_KEY небезопасен ({reason}). Сгенерируйте стойкий ключ: "
            f"`openssl rand -hex 32` и задайте через переменную окружения."
        )
        if self.debug:
            logging.getLogger("app.config").critical(
                "НЕБЕЗОПАСНЫЙ SECRET_KEY: %s — допущено только из-за DEBUG=True",
                msg,
            )
            return self
        raise ValueError(msg)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv\Scripts\python.exe -m pytest tests/unit/test_config_validation.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/unit/test_config_validation.py
git commit -m "fix(security): SECRET_KEY fail-fast при пустом/слабом ключе (аудит C1)"
```

---

## Task 2: H1 — hide blog drafts from non-writers

**Files:**
- Modify: `app/dependencies.py` (add `get_current_user_optional` + `is_writer`)
- Modify: `app/routers/posts.py` (`list_posts`, `get_post` use optional auth)
- Test: `tests/integration/test_blog.py` (add draft-visibility cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_blog.py`:

```python
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
```

(`_make_admin`, `_auth`, `_author`, `_post`, `PostPublish` are already imported/defined in this file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/integration/test_blog.py -q -k draft`
Expected: FAIL — drafts currently appear in list and detail returns 200 for anonymous.

- [ ] **Step 3a: Add optional-auth dependency**

In `app/dependencies.py`, add near `oauth2_scheme`:

```python
# Необязательная аутентификация: для публичных ручек, которым нужно ЗНАТЬ
# пользователя, если токен есть (например, показать черновики блога
# writer'у), но не требовать его (аноним просто получает публичный срез).
# auto_error=False → отсутствие заголовка не даёт 401, отдаёт None.
oauth2_scheme_optional = OAuth2PasswordBearer(
    tokenUrl="/auth/token", auto_error=False
)


async def get_current_user_optional(
    db: AsyncSession = Depends(get_db),
    token: str | None = Depends(oauth2_scheme_optional),
) -> User | None:
    """Текущий пользователь или None. Любая ошибка токена → None (не 401):
    публичная ручка продолжает работать как для анонима."""
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    try:
        uid = UUID(payload.get("sub", ""))
    except (ValueError, TypeError):
        return None
    user = await get_user_by_id(db, uid)
    if user is None or not user.is_active:
        return None
    return user


def is_writer(user: User | None) -> bool:
    """admin или organizer — роль, которой можно писать/видеть черновики блога."""
    if user is None:
        return False
    return any(r.role.value in ("admin", "organizer") for r in user.roles)
```

- [ ] **Step 3b: Use optional auth in blog read endpoints**

In `app/routers/posts.py`, update imports:

```python
from app.dependencies import get_current_user, get_current_user_optional, is_writer, require_any_role
```

Replace `list_posts` body to force published-only for non-writers:

```python
async def list_posts(
    publish: PostPublish | None = Query(
        None, description="Фильтр по статусу (published/draft)"
    ),
    query: str | None = Query(
        None, max_length=200, description="Поиск по title/description/тегам"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    # Аудит H1: публичная витрина отдаёт только published. Черновики видит
    # лишь writer (admin/organizer) и только если сам их запросил.
    if not is_writer(user):
        publish = PostPublish.published
    items = await repo.list_page(
        db, publish=publish, query=query, page=page, per_page=per_page
    )
    total = await repo.count(db, publish=publish, query=query)
    return PostPage(
        items=[to_card(p) for p in items],
        total=total,
        page=page,
        per_page=per_page,
    )
```

Replace `get_post` to 404 drafts for non-writers:

```python
async def get_post(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    post = await repo.get_by_slug(db, slug)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    # Аудит H1: черновик доступен только writer'у; анониму — как будто нет.
    if post.publish != PostPublish.published and not is_writer(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    return to_response(post)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/integration/test_blog.py -q`
Expected: PASS (all blog tests, incl. 3 new draft cases).

- [ ] **Step 5: Commit**

```bash
git add app/dependencies.py app/routers/posts.py tests/integration/test_blog.py
git commit -m "fix(blog): черновики не видны анониму в списке и по slug (аудит H1)"
```

---

## Task 3: M1 — rate-limit public blog list/search

**Files:**
- Modify: `app/routers/posts.py` (`list_posts` gains `check_rate_limit`)
- Test: `tests/integration/test_blog.py` (smoke: under-limit still 200)

- [ ] **Step 1: Write the test**

Append to `tests/integration/test_blog.py`:

```python
async def test_posts_list_under_rate_limit_ok(client, db_session):
    author = await _author(db_session)
    await _post(db_session, author, slug="rl-1", publish=PostPublish.published)
    # Несколько запросов подряд в пределах лимита — все 200 (не 429).
    for _ in range(5):
        r = await client.get("/posts?query=rl")
        assert r.status_code == 200, r.text
```

- [ ] **Step 2: Run it (passes pre-change, guards against regression)**

Run: `venv\Scripts\python.exe -m pytest tests/integration/test_blog.py -q -k rate_limit`
Expected: PASS (no limiter yet; this test pins that normal browsing stays 200 after we add one).

- [ ] **Step 3: Add the rate limit**

In `app/routers/posts.py`, update imports:

```python
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis

from app.redis import get_redis
from app.middleware.progressive_ban import check_rate_limit
```

Add the limiter as the first line of `list_posts` (add `request` and `redis` params):

```python
async def list_posts(
    request: Request,
    publish: PostPublish | None = Query(
        None, description="Фильтр по статусу (published/draft)"
    ),
    query: str | None = Query(
        None, max_length=200, description="Поиск по title/description/тегам"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
    redis: Redis = Depends(get_redis),
):
    # Аудит M1: публичный список + ILIKE-поиск (?query) — DoS-вектор на
    # анонимной ручке. 60/мин на IP щедро для листания, режет флуд. Тот же
    # check_rate_limit, что у /classifieds/search (bug_213). fail-open: при
    # сбое Redis не ломаем публичную витрину (это не auth-ручка).
    await check_rate_limit(request, limit=60, window=60, redis=redis)
    if not is_writer(user):
        publish = PostPublish.published
    items = await repo.list_page(
        db, publish=publish, query=query, page=page, per_page=per_page
    )
    total = await repo.count(db, publish=publish, query=query)
    return PostPage(
        items=[to_card(p) for p in items],
        total=total,
        page=page,
        per_page=per_page,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/integration/test_blog.py -q`
Expected: PASS (all blog tests, incl. rate-limit smoke).

- [ ] **Step 5: Commit**

```bash
git add app/routers/posts.py tests/integration/test_blog.py
git commit -m "fix(blog): rate-limit публичного списка/поиска постов (аудит M1)"
```

---

## Task 4: Full regression run

- [ ] **Step 1: Run the whole integration + unit suite with a STRONG secret**

Run (PowerShell):
```
$env:DATABASE_URL="postgresql+asyncpg://showtail:showtail@localhost:5432/showtail"; $env:REDIS_URL="redis://localhost:6379/0"; $env:SECRET_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"; venv\Scripts\python.exe -m pytest tests -q
```
Expected: all green (46 integration + 5 unit, ± new blog cases).

- [ ] **Step 2: Confirm no migration/import breakage**

Run: `docker exec show-ring-backend-api-1 python -c "import app.main; print('import OK')"`
Expected: `import OK`.

---

## Self-Review

- **Spec coverage:** C1 → Task 1; H1 → Task 2; M1 → Task 3. P1/P2 (M2, L1–L3) intentionally out of this plan (P0 only, per request).
- **Placeholders:** none — every code step shows full code.
- **Type consistency:** `is_writer`/`get_current_user_optional` defined in Task 2 are reused (not redefined) in Task 3; `PostPublish`, `_post`, `_make_admin`, `_auth`, `_author` already exist in the test file.
- **Test-env caveat:** documented — strong `SECRET_KEY` required so Task 1's validator doesn't reject the test key.
