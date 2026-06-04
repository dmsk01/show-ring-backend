# Этап 19 — Безопасность аккаунта (смена email/пароля, аудит)

## Контекст

Фронт уже шлёт `PUT /users/me` с `{ email, current_password }` (раздел «Безопасность»).
Бэк частично готов (bug_203): re-auth по паролю, 409 на коллизию, отзыв refresh.
Этот этап закрывает недостающее: транзакционные письма, подтверждение нового
email через `pending_email`, смену пароля, rate-limit на чувствительных
эндпоинтах, аудит-лог.

**Решения (зафиксированы):**
- Email меняется **через `pending_email`** — новый адрес попадает в `users.email`
  только после клика по ссылке. Старый адрес рабочий до подтверждения.
- forgot/reset password — **отдельным этапом**, не входит сюда.
- Аудит — **новая таблица `security_audit_logs`** (не `ModerationLog`: там actor —
  модератор над чужим контентом; тут actor = сам пользователь, нужны ip/user_agent).

## Карта «что есть / чего нет»

| Фронт-пункт | Состояние |
|---|---|
| PUT /users/me `{email, current_password}` | ✅ bug_203 (re-auth 403, 409, отзыв refresh) |
| …rate-limit на нём | ❌ единственный auth-эндпоинт без `check_rate_limit` |
| Письмо-подтверждение нового email | ❌ только TODO; письмо не уходит |
| Транзакционная отправка писем | ❌ есть только подписочный канал (events_handler) |
| Единый формат ошибок `{detail}` | 🟡 коды разнородны |
| Смена пароля | ❌ нет |
| forgot/reset | ⏭️ отдельный этап |
| Rate-limit на смену email/пароля | ❌ |
| Аудит-лог | ❌ (есть только ModerationLog) |

## Фазы

### Фаза A — Фундамент: транзакционные письма
- **A1** `app/services/email_tasks.py` — `enqueue_transactional_email(db, *, to_email,
  template_name, context)`. Создаёт `Notification(pending)`, рендерит `render_email`,
  кладёт `EmailTaskMessage` в outbox (`exchange=None`, `routing_key="email_tasks"`).
  Та же связка, что `events_handler.process_event`, но без цикла по подписчикам.
- **A2** Шаблоны `app/templates/email/`:
  `email_change_confirm.html.j2`, `password_changed.html.j2`.
- **A3** `app/config.py` — `frontend_base_url` для ссылок в письмах.

### Фаза B — Смена email через `pending_email`
- **B1** Миграция: `users.pending_email VARCHAR(255) NULL`. Токены —
  переиспользуем `email_verification_tokens` (`mark_email_token_used` атомарен).
- **B2** Переписать `PUT /users/me`: добавить `check_rate_limit(limit=5, window=3600,
  fail_closed=True)`; логику смены email вынести в `request_email_change(...)` —
  re-auth, 409 на занятость, запись `pending_email` + токен + письмо. **Не** менять
  `users.email`. Отзыв refresh переносится в момент подтверждения (B3), не запроса.
- **B3** `POST /auth/confirm-email-change` — валидация токена, перенос
  `pending_email → email`, `is_email_verified=True`, `revoke_all_refresh_tokens`.

### Фаза C — Повторная отправка подтверждения
- **C1** `POST /auth/resend-verification` — новый verify-токен + письмо, rate-limit
  `limit=3, window=3600`, анти-enumeration. Заодно закрыть TODO отправки письма
  в `register_user`.

### Фаза D — Смена пароля
- **D1** `PUT /users/me/password` + `change_password(...)`: rate-limit, схема
  `PasswordChange` с `validate_password`, re-auth (403), `new != current` (400),
  `hash_password`, `revoke_all_refresh_tokens_for_user`, письмо `password_changed`
  на текущий адрес, аудит.

### Фаза E — Аудит-лог
- **E1** `app/models/security_audit.py` + миграция `security_audit_logs`
  (`user_id`, `action`, `ip`, `user_agent`, `extra` JSONB, `created_at`) +
  `record_security_event(...)`. ip/user_agent прокидываются из роутера.

### Фаза F — Единый формат ошибок
- **F1** Свести `detail` к машиночитаемым кодам: `current_password_invalid` (403),
  `email_taken` (409), `password_too_weak` (422), `password_same_as_current` (400),
  `invalid_or_expired_token` (400).

## Порядок

```
A (письма) ──> B (email change) ──> C (resend)
            └─> D (password change)
E (аудит) ── вплетается в B2/B3/D1
F (ошибки) ── финальный проход
```

Рекомендуемый: A → E → B → D → C → F.

## Критерии готовности

- [ ] `PUT /users/me` со сменой email защищён rate-limit; `users.email` не меняется,
      заполняется `pending_email`, письмо приходит в MailPit.
- [ ] `POST /auth/confirm-email-change` переносит pending → email, отзывает refresh.
- [ ] Неверный пароль → 403 `current_password_invalid`; занятый email → 409 `email_taken`.
- [ ] `PUT /users/me/password` меняет пароль, отзывает все refresh, шлёт письмо.
- [ ] `POST /auth/resend-verification` шлёт письмо, rate-limit срабатывает (429).
- [ ] `register_user` реально отправляет письмо подтверждения (TODO закрыт).
- [ ] `security_audit_logs` пишется при смене email/пароля (action, ip).
- [ ] Все ошибки — машиночитаемые коды в `{detail}`.
- [ ] forgot/reset — НЕ в этом этапе (вынесено).
