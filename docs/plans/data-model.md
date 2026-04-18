# Модель данных ShowTail

## Обзор

```
                    ┌──────────────────────────────────┐
                    │         СПРАВОЧНИКИ              │
                    │                                  │
                    │  animal_types ← breeds           │
                    │  breed_groups                     │
                    │  show_classes  show_ranks         │
                    │  titles  grades                   │
                    └──────────────────────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          │                        │                        │
          ▼                        ▼                        ▼
   ┌─────────────┐         ┌─────────────┐         ┌─────────────┐
   │   USERS &   │         │   ВЫСТАВКИ  │         │  КОНТЕНТ    │
   │  ПИТОМНИКИ  │         │             │         │             │
   │             │         │  shows      │         │ classifieds │
   │  users      │────────>│  show_rings │         │ ads         │
   │  user_roles │         │  show_judges│         │ support     │
   │  kennels    │         │  show_breeds│         │ payments    │
   │  dogs       │────────>│  show_entries│        │             │
   │  litters    │         │  show_results│        │             │
   │  dog_titles │         │             │         │             │
   └─────────────┘         └─────────────┘         └─────────────┘
```

---

## Таблицы

### Пользователи и безопасность

#### `users`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| email | VARCHAR(255) UNIQUE | |
| password_hash | VARCHAR(255) | bcrypt |
| first_name | VARCHAR(100) | |
| last_name | VARCHAR(100) | |
| phone | VARCHAR(20) | |
| avatar_file_id | FK → files | |
| is_active | BOOLEAN | Для soft-блокировки |
| is_email_verified | BOOLEAN | Email подтверждён |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

> **Роли вынесены в отдельную таблицу** `user_roles` — один пользователь может совмещать несколько ролей (заводчик + судья, организатор + покупатель и т.д.)

#### `user_roles`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | |
| role | ENUM | admin, organizer, breeder, judge, buyer |
| granted_at | TIMESTAMPTZ | Когда выдана роль |
| granted_by | FK → users | NULL = при регистрации |

> UNIQUE(user_id, role) — одна роль не дублируется.

#### `refresh_tokens`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | |
| token_hash | VARCHAR(255) | SHA-256 хеш токена (не сам токен) |
| expires_at | TIMESTAMPTZ | Время истечения |
| is_revoked | BOOLEAN | Для принудительного отзыва |
| created_at | TIMESTAMPTZ | |

> Хранение в БД позволяет отзывать refresh tokens (logout, смена пароля, блокировка).

#### `email_verification_tokens`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | |
| token_hash | VARCHAR(255) | SHA-256 хеш |
| expires_at | TIMESTAMPTZ | Обычно 24 часа |
| used_at | TIMESTAMPTZ | NULL если не использован |
| created_at | TIMESTAMPTZ | |

---

### Питомники и собаки

#### `kennels`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| owner_id | FK → users | Владелец питомника |
| name | VARCHAR(200) | Название питомника |
| registered_name | VARCHAR(200) | Заводская приставка (официальное название в РКФ) |
| animal_type_id | FK → animal_types | Собаки / кошки / ... |
| description | TEXT | О питомнике |
| city | VARCHAR(100) | |
| region | VARCHAR(100) | |
| website | VARCHAR(255) | |
| phone | VARCHAR(20) | |
| is_verified | BOOLEAN | Проверен модератором |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

#### `dogs`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| owner_id | FK → users | Владелец |
| kennel_id | FK → kennels | NULL если не из питомника |
| breed_id | FK → breeds | |
| name | VARCHAR(200) | Кличка |
| registered_name | VARCHAR(200) | Кличка по родословной |
| sex | ENUM | male, female |
| birth_date | DATE | |
| color | VARCHAR(100) | Окрас |
| chip_number | VARCHAR(50) | Номер микрочипа |
| tattoo_number | VARCHAR(50) | Номер клейма |
| pedigree_number | VARCHAR(50) | Номер родословной |
| father_id | FK → dogs | NULL если не в системе |
| mother_id | FK → dogs | NULL если не в системе |
| is_active | BOOLEAN | |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

#### `dog_photos`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| dog_id | FK → dogs | |
| file_id | FK → files | |
| is_main | BOOLEAN | Главное фото |
| sort_order | INT | |

#### `dog_titles`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| dog_id | FK → dogs | |
| title_id | FK → titles | CAC, CACIB, ЛПП, ... |
| show_id | FK → shows | На какой выставке получен |
| judge_id | FK → users | Кто присвоил |
| date_earned | DATE | |
| certificate_file_id | FK → files | Скан сертификата |
| created_at | TIMESTAMPTZ | |

> **`dog_titles` — единственный источник истины о титулах.** JSONB `titles` в `show_results` — кэш для быстрого отображения.

---

### Помёты

#### `litters`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| kennel_id | FK → kennels | |
| breed_id | FK → breeds | |
| father_id | FK → dogs | Отец |
| mother_id | FK → dogs | Мать |
| birth_date | DATE | Дата рождения помёта |
| puppies_total | INT | Всего щенков |
| puppies_available | INT | Доступно к продаже |
| price_min | DECIMAL | Цена от |
| price_max | DECIMAL | Цена до |
| description | TEXT | |
| status | ENUM | planned, born, available, sold |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

---

### Выставки

#### `shows`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| organizer_id | FK → users | Организатор (клуб) |
| animal_type_id | FK → animal_types | |
| rank_id | FK → show_ranks | CACIB, CAC ЧФ, ... |
| name | VARCHAR(300) | "КУБОК ВАЛТА - 2026" |
| description | TEXT | |
| city | VARCHAR(100) | |
| venue | VARCHAR(300) | Место проведения |
| date_start | DATE | |
| date_end | DATE | |
| registration_start | TIMESTAMPTZ | Открытие записи |
| registration_end | TIMESTAMPTZ | Закрытие записи |
| entry_fee | DECIMAL | Стоимость участия |
| status | ENUM | draft, registration_open, registration_closed, in_progress, completed, cancelled |
| catalog_file_id | FK → files | Сгенерированный каталог |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

#### `show_breeds`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| show_id | FK → shows | |
| breed_id | FK → breeds | |

> Породы, допущенные к участию в выставке. Пустая таблица = все породы (всепородная выставка). Заполненная = монопородная / ограниченный список.

#### `show_judges`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| show_id | FK → shows | |
| judge_id | FK → users | |
| breed_group_id | FK → breed_groups | NULL = все группы |
| breed_id | FK → breeds | NULL = вся группа |
| is_main_ring | BOOLEAN | Судья главного ринга (BIS) |

#### `show_rings`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| show_id | FK → shows | |
| ring_number | INT | Номер ринга |
| judge_id | FK → users | |
| breed_id | FK → breeds | Порода в ринге |
| show_class_id | FK → show_classes | Класс (юниоры, открытый, ...) |
| start_time | TIMESTAMPTZ | Время начала |
| estimated_duration | INTERVAL | Примерная длительность |

#### `show_entries`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| show_id | FK → shows | |
| dog_id | FK → dogs | |
| owner_id | FK → users | Кто записал |
| show_class_id | FK → show_classes | Класс — **выбирается владельцем** из доступных по возрасту |
| catalog_number | INT | Номер в каталоге |
| is_paid | BOOLEAN | Оплачено |
| status | ENUM | pending, confirmed, cancelled, no_show |
| created_at | TIMESTAMPTZ | |

> **Класс не определяется автоматически.** Система вычисляет список допустимых классов по возрасту собаки, владелец выбирает. Например, собака 16 месяцев может пойти в юниоры, промежуточный или открытый.

#### `show_results`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| show_entry_id | FK → show_entries | |
| grade_id | FK → grades | Оценка (отлично, оч.хорошо, ...) |
| placement | INT | Место в расстановке (1, 2, 3, 4) |
| titles_cache | JSONB | **Кэш** присвоённых титулов для быстрого отображения. Источник истины — `dog_titles` |
| critique | TEXT | Описание от судьи |
| is_best_of_breed | BOOLEAN | BOB / ЛПП |
| is_best_of_group | BOOLEAN | BIG |
| is_best_in_show | BOOLEAN | BIS |
| judge_id | FK → users | |
| created_at | TIMESTAMPTZ | |

> При вводе результатов: 1) запись в `show_results`, 2) запись в `dog_titles`, 3) обновление `titles_cache` — всё в одной транзакции.

---

### Доска объявлений

#### `classifieds`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| author_id | FK → users | |
| category | ENUM | puppy_sale, stud_service, handler, grooming, other |
| animal_type_id | FK → animal_types | |
| breed_id | FK → breeds | NULL если не привязано к породе |
| title | VARCHAR(300) | |
| description | TEXT | |
| price | DECIMAL | NULL если "договорная" |
| city | VARCHAR(100) | |
| region | VARCHAR(100) | |
| status | ENUM | active, moderation, closed, archived |
| is_promoted | BOOLEAN | Платное поднятие |
| views_count | INT | |
| expires_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

#### `classified_images`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| classified_id | FK → classifieds | |
| file_id | FK → files | |
| sort_order | INT | |

---

### Платежи

#### `payments`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | Плательщик |
| amount | DECIMAL(10,2) | Сумма |
| currency | VARCHAR(3) | "RUB" |
| status | ENUM | pending, processing, completed, failed, refunded |
| payment_type | ENUM | show_entry, promotion, ad_campaign |
| entity_id | UUID | ID связанной сущности (show_entry, classified, ad_campaign) |
| provider | VARCHAR(50) | "yookassa", "robokassa", "manual" |
| provider_payment_id | VARCHAR(200) | ID транзакции во внешней системе |
| paid_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

> Интеграция с платёжным шлюзом реализуется позже. Таблица закладывается для ручного подтверждения оплаты организатором и будущей автоматизации.

---

### Реклама

#### `ad_campaigns`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| advertiser_id | FK → users | |
| name | VARCHAR(200) | |
| budget | DECIMAL | Бюджет |
| spent | DECIMAL | Потрачено |
| status | ENUM | draft, active, paused, completed |
| start_date | DATE | |
| end_date | DATE | |
| created_at | TIMESTAMPTZ | |

#### `ad_banners`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| campaign_id | FK → ad_campaigns | |
| image_file_id | FK → files | |
| target_url | VARCHAR(500) | Ссылка при клике |
| placement | ENUM | header, sidebar, catalog, show_page, breed_page |
| target_animal_type_id | FK → animal_types | Таргетинг |
| target_breed_id | FK → breeds | NULL = все породы |
| target_region | VARCHAR(100) | NULL = все регионы |
| impressions_count | INT | |
| clicks_count | INT | |
| is_active | BOOLEAN | |

#### `ad_events`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | BIGSERIAL (PK) | Автоинкремент для высокой нагрузки |
| banner_id | FK → ad_banners | |
| event_type | ENUM | impression, click |
| user_id | FK → users | NULL если аноним |
| ip_address | INET | |
| user_agent_hash | VARCHAR(64) | SHA-256 хеш User-Agent (для дедупликации) |
| page_url | VARCHAR(500) | |
| created_at | TIMESTAMPTZ | Партиционирование по дате |

> **Защита от фрода:** дедупликация по (banner_id, ip_address, user_agent_hash, event_type) в окне 60 секунд. Реализуется через Redis SET с TTL в воркере.

---

### Уведомления и подписки

#### `notifications`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | |
| type | VARCHAR(50) | email, push |
| subject | VARCHAR(300) | |
| status | ENUM | pending, sent, failed |
| sent_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |

#### `subscriptions`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | |
| event_type | VARCHAR(50) | show_opened, litter_announced, ... |
| filter_breed_id | FK → breeds | NULL = все породы |
| filter_region | VARCHAR(100) | NULL = все регионы |
| channel | ENUM | email, push |
| is_active | BOOLEAN | |

---

### Онлайн-поддержка

#### `support_tickets`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| user_id | FK → users | |
| subject | VARCHAR(300) | |
| status | ENUM | open, in_progress, resolved, closed |
| priority | ENUM | low, normal, high |
| assigned_to_id | FK → users | Оператор |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

#### `support_messages`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| ticket_id | FK → support_tickets | |
| sender_id | FK → users | |
| message | TEXT | |
| is_from_operator | BOOLEAN | |
| created_at | TIMESTAMPTZ | |

---

### Файлы

#### `files`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| uploader_id | FK → users | |
| original_name | VARCHAR(255) | Имя при загрузке |
| stored_path | VARCHAR(500) | Путь в S3/MinIO |
| mime_type | VARCHAR(100) | image/jpeg, application/pdf, ... |
| size_bytes | BIGINT | |
| created_at | TIMESTAMPTZ | |

> **Связи с файлами — через FK на стороне владельца**, а не через полиморфные поля в `files`:
> - `users.avatar_file_id → files` (аватар)
> - `shows.catalog_file_id → files` (каталог)
> - `dog_titles.certificate_file_id → files` (сертификат)
> - `ad_banners.image_file_id → files` (баннер)
> - `dog_photos` (фото собак, many-to-many)
> - `classified_images` (фото объявлений, many-to-many)
>
> Это обеспечивает FK constraints и целостность данных.

### Задачи (фоновые)

#### `tasks`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID (PK) | |
| type | VARCHAR(50) | generate_catalog, generate_diploma, send_email, ... |
| status | ENUM | pending, processing, done, failed |
| payload | JSONB | Входные данные |
| result | JSONB | Результат (file_id, error, ...) |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

---

### Справочники

#### `animal_types`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL (PK) | |
| name | VARCHAR(50) | "Собаки", "Кошки" |
| code | VARCHAR(20) UNIQUE | "dog", "cat" |

#### `breed_groups`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL (PK) | |
| animal_type_id | FK → animal_types | |
| number | INT | Номер группы FCI (1-10) |
| name | VARCHAR(200) | "Пастушьи и скотогонные" |

#### `breeds`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL (PK) | |
| animal_type_id | FK → animal_types | |
| breed_group_id | FK → breed_groups | |
| name | VARCHAR(200) | "Немецкая овчарка" |
| name_en | VARCHAR(200) | "German Shepherd Dog" |
| fci_number | INT | Номер стандарта FCI |
| country | VARCHAR(100) | Страна происхождения |

#### `show_ranks`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL (PK) | |
| animal_type_id | FK → animal_types | |
| code | VARCHAR(20) | "CACIB", "CAC_CHRKF", "CAC_CHF", ... |
| name | VARCHAR(100) | "Интернациональная CACIB FCI" |
| level | INT | Уровень ранга (для сортировки) |

#### `show_classes`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL (PK) | |
| animal_type_id | FK → animal_types | |
| code | VARCHAR(20) | "baby", "puppy", "junior", ... |
| name | VARCHAR(100) | "Класс юниоров" |
| age_from_months | INT | Минимальный возраст (мес.) |
| age_to_months | INT | NULL = без верхней границы |
| can_receive_cac | BOOLEAN | Может ли получить CAC |
| sort_order | INT | Порядок в расписании |

#### `titles`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL (PK) | |
| animal_type_id | FK → animal_types | |
| code | VARCHAR(20) | "CW", "CAC", "R_CAC", "CACIB", "BOB", "BIS", ... |
| name | VARCHAR(100) | "Победитель класса" |
| name_en | VARCHAR(100) | "Class Winner" |
| category | ENUM | class_title, breed_title, group_title, show_title |

#### `grades`
| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL (PK) | |
| animal_type_id | FK → animal_types | |
| code | VARCHAR(20) | "excellent", "very_good", "good", ... |
| name | VARCHAR(100) | "Отлично" |
| name_en | VARCHAR(100) | "Excellent" |
| for_puppies | BOOLEAN | Щенячья оценка или взрослая |
| sort_order | INT | |
