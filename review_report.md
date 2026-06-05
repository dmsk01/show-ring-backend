# Code Review Report
**Date:** 2026-06-05
**Project:** ShowTail — платформа управления выставками животных (FastAPI async, PostgreSQL/asyncpg, SQLAlchemy 2.0, RabbitMQ/aio-pika, Redis, MinIO)

## Executive Summary

ShowTail is a mature, heavily-reviewed backend. The code carries an unusually
dense trail of prior audit fixes (`bug_2xx audit 2026-05-28`, `ultrareview`,
`review 2026-06-01`) and the inline comments explaining each decision are
accurate and worth respecting. I read those comments before flagging anything;
where a comment already addresses a concern I either dropped the finding or
quote the comment below.

The high-value security surfaces are solid:

- **Auth** — bcrypt with constant-time dummy verify against user-enumeration,
  refresh-token rotation with reuse-detection, JWT decoded with explicit
  `require_exp/require_sub`, re-auth required for email/password changes, all
  auth endpoints `fail_closed=True` on rate-limit.
- **Injection** — no f-string SQL anywhere; raw SQL (`analytics.py`,
  `classified.py` FTS) uses bound `:params` exclusively; ORM filters use Core
  expressions. DOCX template names are hardcoded literals (no path traversal).
- **Files** — magic-bytes validation (not Content-Type), private documents
  gated behind owner/admin ACL with `is_public=False`, `Content-Disposition`
  hardened via RFC 6266 `filename*` (closes the bug_202 header-injection class).
- **Async hygiene** — CPU-bound work (docxtpl, Pillow) is correctly offloaded
  with `asyncio.to_thread`; one DB session per message in the worker.

**No critical (RCE / SQLi / auth-bypass) issues were found.** The headline
finding is a stale-import that left the support-chat Redis pub/sub permanently
disabled (now fixed and covered by tests); the rest are a packaging/repro
inconsistency and a set of minor doc/hygiene items.

**Code fixes applied in this pass:** `ws_manager` stale-binding (Major, +tests),
rate-limiter sorted-set member collision, and two stale comments
(`main.py` middleware order, `worker/main.py` ack semantics). The
packaging/pinning items below are left as decisions for the team, not changed.

---

## Critical Issues 🔴

None found.

---

## Major Issues 🟠

### [STALE-BINDING] WebSocket manager never engaged Redis pub/sub — `from app.redis import redis_client` captured a permanent `None` — ✅ FIXED
- File: `app/services/ws_manager.py:48` (import), `:101`/`:128`/`:171` (uses)
- Description: `ws_manager.py` imported the **value**
  (`from app.redis import redis_client`), binding the name to whatever
  `app.redis.redis_client` was at import time — i.e. `None`, since
  `init_redis()` only assigns the real client later, during app startup. A
  `from … import name` binding does not track that reassignment, so
  `redis_client` stayed `None` for the life of the process. Effects on the
  support chat:
  - `connect()` — `redis_client is not None` was always `False` → the instance
    **never subscribed** to the Redis channel; `_listen` never ran.
  - `publish()` — always fell through to `_broadcast_local` → messages reached
    only sockets on the *same* API instance; **cross-instance pub/sub was
    silently dead** (broken the moment you `--scale api=2`).
- Note: This is exactly the stale-binding trap that `health.py` explicitly
  avoids, with a documented comment: *"Импортируем модуль, а не значение:
  init_redis() пере-присваивает app.redis.redis_client уже после импорта.
  `from app.redis import redis_client` связал бы стейл-None…"* (`health.py:30`).
  `ws_manager` simply didn't follow the same rule.
- Correction to the first draft of this report: an earlier version flagged a
  *race* in `connect()` (need_subscribe read in-lock, task registered out-of-lock)
  as the Major issue. On closer analysis that race is **not reachable** under
  single-threaded asyncio — there is no `await` between the in-lock read and the
  out-of-lock write, so two `connect()` coroutines cannot interleave there — and
  it was moot anyway because `redis_client` was always `None`. The real defect is
  the import above.
- Fix applied: import the module (`from app import redis as redis_state`) and
  reference `redis_state.redis_client` in `connect`/`publish`/`_listen`. The
  subscribe-registration was also consolidated fully inside `self._lock` as
  defensive hardening (clearer, robust if an `await` is ever added to the block),
  not because a live race existed.
- Tests: `tests/unit/test_ws_manager.py` (6 cases, fake Redis pub/sub + fake
  sockets, no infra needed) now covers first-connect subscription, single
  subscription under concurrent connects, exactly-once delivery, JSON publish
  through Redis, and disconnect-cancels-subscription. These would fail on the
  stale-import version (the patched `app.redis.redis_client` never reaches the
  manager). Full unit suite: **80 passed**.

### [BUILD/DEPENDENCIES] Committed `packages/` wheel cache is incomplete and does not match the documented offline-install flow
- File: `requirements.txt:4`, `scripts/download_packages.sh:21`, `packages/`
- Description: `requirements.txt` declares the runtime deps and the header says
  *"Для офлайн-установки: scripts/download_packages.sh"*. But:
  - The download script writes to **`offline_packages/`**, while the directory
    actually committed to git is **`packages/`** — two different names.
  - `packages/` is **missing every heavy runtime dependency that the app imports
    at module load**: `passlib`, `python-jose`, `bcrypt`, `boto3`, `aioboto3`,
    `Pillow`, `aiofiles`, `python-magic`, `reportlab`, `docxtpl`, `aiosmtplib`,
    `APScheduler`, `structlog`, `bleach` (verified: none present). The wheels that
    *are* committed are a partial set (fastapi, pydantic_core, starlette, redis,
    jinja2, httpx, uvicorn, …), several platform-pinned (`cp313-win_amd64`).
  - Therefore `pip install --no-index --find-links=packages/ -r requirements.txt`
    cannot succeed, and even if it did the app would `ImportError` on first load
    (`app/utils/security.py` imports passlib/jose; `app/middleware/sanitization.py`
    imports bleach; `app/services/file_storage.py` imports aioboto3).
- Note: This is not a correctness bug in the running app (a normal online
  `pip install` works), but it makes the repo misleading and bloats history with
  ~70 binary wheels, including OS/Python-specific ones that won't install on Linux
  containers (the Dockerfile builds on its own).
- Suggestion: Decide on one mechanism. Either (a) remove `packages/` from git,
  add it to `.gitignore`, and rely on `download_packages.sh` → `offline_packages/`
  on demand; or (b) if a committed offline cache is genuinely required, regenerate
  it from the full `requirements.txt` for the target platform and rename it to
  match the script. Don't ship a partial, platform-pinned cache.

---

## Minor Issues 🟡

### [DEPENDENCIES] Unpinned versions / no lockfile
- File: `requirements.txt:8`, `requirements-dev.txt`
- Description: All deps use `>=` floors with no upper bound and no lockfile
  (`requirements.lock` / `pip-tools` / hashes). Builds are non-reproducible and
  exposed to a breaking or malicious upstream release. The codebase already shows
  the cost of this (`bcrypt<4.1` had to be capped because passlib broke — good
  catch, but it argues for pinning the rest too).
- Note: `requirements.txt:19` documents one real cap with rationale:
  *"passlib 1.7.4 несовместим с bcrypt>=4.1 (ломает verify: 'password > 72 bytes')"* —
  respect that line; the suggestion is to extend the discipline, not change it.
- Suggestion: Generate a hashed lockfile (`pip-compile --generate-hashes`) and
  install from it in CI/Docker; keep `requirements.txt` as the human-edited input.

### [HYGIENE] Binary wheels checked into version control
- File: `packages/*.whl`
- Description: ~70 `.whl` files (incl. compiled `cp313-cp313-win_amd64`) live in
  git. This bloats clones, churns history on every dep bump, and ties artifacts to
  one interpreter/OS. See the Major packaging finding for the consolidation plan.
- Suggestion: Move to `.gitignore`; fetch on demand.

### [DOCS] Idempotency middleware runs *before* Sanitization, contradicting its own ordering comment
- File: `app/main.py:109`, `app/middleware/idempotency.py:119`
- Description: The comment in `main.py` states the intended order is
  *"3. Idempotency — после sanitization (тело уже чистое), но до handler'а"*.
  Given Starlette executes middleware in reverse-addition order, the actual
  request-path order is `ProxyHeaders → SecurityHeaders → Idempotency →
  Sanitization → RequestId → handler` — i.e. Idempotency sees the **raw**
  (un-sanitized) body, not a clean one. This is harmless for correctness (the
  body hash is computed on the raw body consistently across retries, and the
  handler still receives the sanitized body), but the documented rationale is
  wrong and will mislead the next maintainer who reasons about ordering.
- Suggestion: Fix the comment to describe the real order, or, if "hash the
  sanitized body" is actually desired, move `add_middleware(IdempotencyMiddleware)`
  to be added *before* `SanitizationMiddleware`.

### [DOCS] Worker message docstrings contradict the swallow-and-ack code
- File: `worker/main.py:67`, `worker/main.py:96`
- Description: `on_document_message` / `on_image_message` docstrings say
  *"Если хендлер бросает исключение наружу, ack не делается и RabbitMQ
  переотправит сообщение позднее"*, but the body wraps the call in
  `try/except Exception: logger.exception(...)`, so the exception never escapes
  `message.process(requeue=False)` → the message is **always** acked. The handler
  itself records `task.status='failed'`, so the behaviour is intentional and fine;
  only the docstring is stale and self-contradictory.
- Suggestion: Update the docstring to say the message is acked regardless and the
  failure is persisted in `task.status`, matching the actual `requeue=False`
  design.

### [CORRECTNESS] Rate-limit sorted-set member can collide under same-instant bursts
- File: `app/middleware/progressive_ban.py:56`
- Description: The Lua script stores each request as
  `zadd(rate_key, now, tostring(now))` where `now = time.time()`. If two requests
  from one IP land in the same float instant, they map to the same member and
  `ZADD` updates the score instead of adding a second entry → the window
  under-counts by one. Extremely unlikely at human request rates, but it slightly
  weakens the limiter exactly under burst conditions, which is when it matters.
- Suggestion: Make the member unique, e.g. append a counter:
  `redis.call('zadd', rate_key, now, now .. ':' .. redis.call('incr', seq_key))`,
  or include the request id. Keep the score as `now` for the sliding window.

### [SECURITY/CONFIG] Weak default credentials in docker-compose
- File: `docker-compose.yml:36`, `:55`, `:88`
- Description: `POSTGRES_PASSWORD:-showtail`, `RABBITMQ_DEFAULT_PASS:-guest`,
  `MINIO_ROOT_PASSWORD:-showtailminio` default to weak values if env vars are
  unset. `SECRET_KEY` is correctly *required* (`${SECRET_KEY}` with no default),
  and `.env.example:SECRET_KEY=change-me-in-production` is an obvious placeholder.
- Note: The compose file already documents that the host-exposed PG port is for
  dev convenience (`docker-compose.yml:38`), so this is mostly a prod-deploy
  reminder, not a code defect.
- Suggestion: Document in the deploy runbook that all `*_PASSWORD`/`*_KEY` must be
  set in prod, or drop the `:-default` fallbacks for the secret-bearing vars so a
  missing value fails loudly instead of silently using `guest`.

### [TESTING] Critical infrastructure paths lack tests
- File: `tests/integration/` (no coverage for the modules below)
- Description: Integration tests exist for auth flow, classifieds, delete
  endpoints, file ACL, notifications-read and showcase — good coverage of
  business endpoints. But several **security/availability-critical** mechanisms
  have no visible tests:
  - refresh-token **rotation + reuse-attack revocation** (`services/auth.py:185`),
  - the **idempotency** middleware (cache hit, in-flight 409, body-size skip),
  - the **progressive rate-limiter** Lua path and `fail_closed` behaviour,
  - ~~the **ws_manager** pub/sub fan-out~~ — ✅ now covered by
    `tests/unit/test_ws_manager.py` (added in this pass; a subscription test
    caught the stale-binding bug).
- Suggestion: Add focused tests for refresh rotation/reuse and the rate-limiter
  next (highest remaining security value).

---

## Positive observations ✅

- **User-enumeration hardening is consistent end-to-end**: register/resend/login
  all return identical responses regardless of account existence, with a
  constant-time `dummy_verify_password()` on the no-user login path
  (`services/auth.py:152`, `utils/security.py:37`).
- **Refresh-token rotation with reuse detection** revokes the whole chain on a
  replayed token (`services/auth.py:204`) — textbook defense-in-depth.
- **N+1 elimination is deliberate and documented**: the official-document builders
  bulk-load every relation via `_load_map` / `selectinload` instead of per-row
  `db.get` (`services/document_official.py:43`), turning "thousands of round-trips
  for a 1000-dog catalog" into a handful of `WHERE id IN (...)` queries.
- **Transactional outbox** with `SELECT … FOR UPDATE SKIP LOCKED`
  (`repositories/outbox.py:48`) lets multiple dispatchers run safely in parallel.
- **Atomic Redis rate-limiting** moved into a single Lua script to close a
  genuine check-then-act race (`middleware/progressive_ban.py:20`).
- **Jinja autoescape bug fix** for the `.j2` extension is exactly right and the
  comment explains the silent-XSS it closed (`services/email.py:43`).
- **Poison-message caps** (`MAX_TASK_ATTEMPTS`) + DLX queues prevent stuck tasks
  from looping forever (`worker/handlers/document_handler.py:38`).
- **Blocking work offloaded** with `asyncio.to_thread` for both docxtpl and Pillow
  — no event-loop stalls in the worker.
- **No bare `except:`**, no mutable default args, no wildcard imports observed;
  every broad `except Exception` is annotated and intentional.

---

## Recommendations

1. ✅ **Done — `ws_manager` stale-binding fixed** (module import + tests). This
   was the only finding affecting a running instance: support-chat pub/sub now
   actually engages Redis instead of silently degrading to local-only delivery.
2. **Resolve the packaging story** — either delete `packages/` from git or
   regenerate a complete, platform-correct cache that matches
   `download_packages.sh`. Right now the offline path is broken and the repo
   carries misleading binary artifacts.
3. **Introduce a hashed lockfile** for reproducible, supply-chain-safe builds;
   keep the well-reasoned `bcrypt<4.1` pin.
4. **Reconcile the stale comments** in `main.py` (middleware order) and
   `worker/main.py` (ack semantics) so future maintainers trust the comments —
   which, across the rest of this codebase, are genuinely excellent.
5. **Backfill tests** for refresh rotation, the rate-limiter, idempotency, and a
   concurrent-connect case for `ws_manager`.
