# Этап 10: Рекламный модуль

### Цель

Реализовать рекламную систему: управление кампаниями и баннерами, таргетинг, показ рекламы, сбор аналитики через RabbitMQ.

### Что появляется в проекте

- Рекламные кампании: бюджет, период, статус
- Баннеры: изображение, ссылка, позиция размещения
- Таргетинг: по виду животного, породе, региону, странице
- Промо-размещения: платное поднятие объявлений в выдаче
- Показ баннера — API выбирает подходящий баннер по контексту страницы
- Сбор событий (impression/click) через RabbitMQ для аналитики
- Дашборд рекламодателя: показы, клики, CTR, расход бюджета

### Модель данных

Новые таблицы: `ad_campaigns`, `ad_banners`, `ad_events`

Ключевые поля:
- ad_campaigns: advertiser_id, budget, spent, status, даты
- ad_banners: campaign_id, image, target_url, placement, таргетинг (animal_type, breed, region)
- ad_events: banner_id, event_type (impression/click), user_id, ip, page_url, timestamp
  - **Партиционирование по дате** для высокой нагрузки записи

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| POST | `/ads/campaigns` | Создать кампанию | Advertiser |
| GET | `/ads/campaigns` | Мои кампании | Advertiser |
| PUT | `/ads/campaigns/{id}` | Обновить | Advertiser |
| POST | `/ads/campaigns/{id}/banners` | Добавить баннер | Advertiser |
| PUT | `/ads/banners/{id}` | Обновить баннер | Advertiser |
| GET | `/ads/serve` | Получить баннер для показа | Public |
| POST | `/ads/events` | Зафиксировать impression/click | Public |
| GET | `/ads/campaigns/{id}/stats` | Статистика кампании | Advertiser |
| POST | `/classifieds/{id}/promote` | Промо-поднятие объявления | Authenticated |

### Логика показа баннера (GET /ads/serve)

Параметры запроса: `placement`, `animal_type_id`, `breed_id`, `region`

```
1. SELECT активные баннеры WHERE:
   - campaign.status = active
   - campaign.spent < campaign.budget
   - banner.placement = requested_placement
   - banner.is_active = true
   - таргетинг совпадает (или NULL = все)
2. Выбрать случайный из подходящих (или по приоритету)
3. Вернуть: image_url, target_url, banner_id
4. Клиент при показе вызывает POST /ads/events (impression)
5. При клике — POST /ads/events (click)
```

### Сбор аналитики через RabbitMQ

```
Frontend → POST /ads/events → API → publish → RabbitMQ → Worker → Fraud check (Redis)
                                                                 → INSERT ad_events
                                                                 → UPDATE ad_banners SET impressions_count += 1
                                                                 → UPDATE ad_campaigns SET spent += cost_per_impression
```

Зачем через очередь:
- Высокая частота событий (каждый показ страницы)
- Не блокировать API на записи аналитики
- Batch INSERT для эффективности

### Защита от Ad Fraud

```
Дедупликация в воркере (ad_handler.py):

1. Ключ: f"ad_dedup:{banner_id}:{ip}:{user_agent_hash}:{event_type}"
2. Redis SET с TTL 60 секунд
3. Если ключ существует — событие отбрасывается (дубль)
4. Если нет — записываем в ad_events и обновляем счётчики

Дополнительно:
- Progressive ban на POST /ads/events: 100 запросов/мин на IP
- user_agent_hash (SHA-256) хранится в ad_events для анализа
- При превышении — 429 + Retry-After
```

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `app/models/ad.py` | AdCampaign, AdBanner, AdEvent ORM |
| `app/schemas/ad.py` | Pydantic-схемы |
| `app/routers/ads.py` | CRUD + serve + events + stats |
| `app/services/ad.py` | Логика подбора баннера, таргетинг |
| `app/repositories/ad.py` | SQL: подбор, аналитика |
| `worker/handlers/ad_handler.py` | Batch-запись событий, обновление счётчиков |

### Ключевые концепции

- **Таргетинг** — фильтрация баннеров по контексту
- **Batch processing** — воркер накапливает события и пишет пачкой
- **Партиционирование** — ad_events по месяцам для производительности
- **Бюджет** — атомарное списание: `UPDATE SET spent = spent + :cost WHERE spent + :cost <= budget`
- **Ad fraud protection** — дедупликация событий через Redis SET с TTL (IP + user_agent + banner + 60 сек)

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| Партиционирование (Raw SQL) | `CREATE TABLE ad_events ... PARTITION BY RANGE (created_at)` |
| Агрегация по дням (Raw SQL) | `SELECT date_trunc('day', created_at), COUNT(*) ... GROUP BY 1` |
| Conditional UPDATE | `UPDATE SET spent = spent + :x WHERE spent + :x <= budget` (атомарный бюджет) |
| Batch INSERT | `INSERT INTO ad_events VALUES (...), (...), (...)` через executemany |
| CTR расчёт | `SELECT clicks::float / NULLIF(impressions, 0) AS ctr` |

### Как проверить

1. `POST /ads/campaigns` — создать кампанию с бюджетом
2. `POST /ads/campaigns/{id}/banners` — добавить баннер с таргетингом на породу
3. `GET /ads/serve?placement=sidebar&breed_id=5` — получить подходящий баннер
4. `POST /ads/events` — зафиксировать impression
5. Проверить: воркер записал событие в ad_events
6. `GET /ads/campaigns/{id}/stats` — показы, клики, CTR, расход
