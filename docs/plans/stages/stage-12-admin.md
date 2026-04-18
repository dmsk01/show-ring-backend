# Этап 12: Админка и аналитика

### Цель

Реализовать административную панель: модерация контента, верификация питомников, управление рекламой, аналитические дашборды на Raw SQL.

### Что появляется в проекте

- Модерация объявлений: просмотр на модерации, одобрение/отклонение
- Верификация питомников: проверка документов, подтверждение статуса
- Управление пользователями: блокировка, смена роли
- Управление рекламными кампаниями: одобрение, приостановка
- Аналитические дашборды:
  - Статистика платформы (пользователи, питомники, собаки, выставки)
  - Статистика выставок (участники по породам, по регионам)
  - Рекламная аналитика (показы, клики, доход)
  - Отчёт организатора (сводка по конкретной выставке)

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| GET | `/admin/dashboard` | Общая статистика платформы | Admin |
| GET | `/admin/moderation/classifieds` | Объявления на модерации | Admin |
| PUT | `/admin/moderation/classifieds/{id}` | Одобрить/отклонить | Admin |
| GET | `/admin/moderation/kennels` | Питомники на верификации | Admin |
| PUT | `/admin/moderation/kennels/{id}/verify` | Верифицировать | Admin |
| GET | `/admin/users` | Список пользователей | Admin |
| PUT | `/admin/users/{id}/block` | Заблокировать | Admin |
| PUT | `/admin/users/{id}/role` | Сменить роль | Admin |
| GET | `/admin/analytics/shows` | Аналитика выставок | Admin |
| GET | `/admin/analytics/ads` | Рекламная аналитика | Admin |
| GET | `/admin/analytics/shows/{id}/report` | Отчёт по выставке | Admin / Organizer |

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/routers/admin/moderation.py` | Модерация контента |
| `app/routers/admin/analytics.py` | Аналитические эндпоинты |
| `app/services/moderation.py` | Логика модерации |
| `app/repositories/analytics.py` | Raw SQL аналитические запросы |

### Аналитические запросы (Raw SQL)

**Статистика платформы:**
```sql
SELECT
    (SELECT COUNT(*) FROM users WHERE is_active) AS total_users,
    (SELECT COUNT(*) FROM kennels WHERE is_verified) AS verified_kennels,
    (SELECT COUNT(*) FROM dogs) AS total_dogs,
    (SELECT COUNT(*) FROM shows WHERE status = 'completed') AS completed_shows,
    (SELECT COUNT(*) FROM classifieds WHERE status = 'active') AS active_classifieds
```

**Топ пород по количеству участий в выставках:**
```sql
SELECT b.name, COUNT(se.id) AS entries_count
FROM show_entries se
JOIN dogs d ON se.dog_id = d.id
JOIN breeds b ON d.breed_id = b.id
WHERE se.created_at >= :period_start
GROUP BY b.id, b.name
ORDER BY entries_count DESC
LIMIT 20
```

**Сводный отчёт выставки:**
```sql
SELECT
    b.name AS breed,
    sc.name AS class,
    COUNT(se.id) AS entries,
    COUNT(sr.id) FILTER (WHERE sr.grade_id = :excellent) AS excellent_count,
    SUM(CASE WHEN se.is_paid THEN s.entry_fee ELSE 0 END) AS revenue
FROM show_entries se
JOIN dogs d ON se.dog_id = d.id
JOIN breeds b ON d.breed_id = b.id
JOIN show_classes sc ON se.show_class_id = sc.id
LEFT JOIN show_results sr ON sr.show_entry_id = se.id
JOIN shows s ON se.show_id = s.id
WHERE se.show_id = :show_id
GROUP BY b.id, b.name, sc.id, sc.name, sc.sort_order
ORDER BY sc.sort_order, b.name
```

**Рекламная аналитика по дням:**
```sql
SELECT
    date_trunc('day', ae.created_at) AS day,
    COUNT(*) FILTER (WHERE ae.event_type = 'impression') AS impressions,
    COUNT(*) FILTER (WHERE ae.event_type = 'click') AS clicks,
    ROUND(
        COUNT(*) FILTER (WHERE ae.event_type = 'click')::numeric /
        NULLIF(COUNT(*) FILTER (WHERE ae.event_type = 'impression'), 0) * 100, 2
    ) AS ctr_percent
FROM ad_events ae
WHERE ae.created_at >= :period_start
GROUP BY 1
ORDER BY 1 DESC
```

### Ключевые концепции

- **Raw SQL для аналитики** — ORM избыточен для сложных агрегаций
- **FILTER (WHERE ...)** — PostgreSQL conditional aggregation
- **Materialized Views** — для тяжёлых дашбордов (опционально)
- **Role-based access** — все эндпоинты только для admin (и частично organizer)

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| Подзапросы в SELECT | Статистика платформы |
| FILTER (WHERE ...) | Условная агрегация (PostgreSQL-specific) |
| Complex GROUP BY | Отчёт по выставке: порода + класс |
| Window functions | Рейтинги, нарастающие итоги |
| date_trunc | Группировка по дням/неделям/месяцам |
| EXPLAIN ANALYZE | Оптимизация медленных аналитических запросов |

### Как проверить

1. `GET /admin/dashboard` — общая статистика
2. `GET /admin/moderation/classifieds` — объявления на модерации
3. `PUT /admin/moderation/classifieds/{id}` — одобрить
4. `GET /admin/analytics/shows/{id}/report` — сводка по выставке
5. `GET /admin/analytics/ads?period=last_30_days` — рекламная аналитика
6. Проверить время ответа аналитических запросов через `EXPLAIN ANALYZE`
