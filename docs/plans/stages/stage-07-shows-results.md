# Этап 7: Выставки — проведение и результаты

### Цель

Реализовать ввод оценок и расстановок в ринге, автоматическое присвоение титулов по правилам РКФ, публикацию результатов.

### Что появляется в проекте

- Ввод результатов ринга: оценка + место + описание от судьи
- Автоматическое присвоение титулов по правилам РКФ:
  - CW (победитель класса) → автоматически CAC в промежуточном/открытом/рабочем/чемпионов
  - ЛК / ЛС (лучший кобель / сука породы)
  - BOB / ЛПП (лучший представитель породы)
  - BIG (лучший в группе FCI)
  - BIS (лучшая собака выставки)
- Титулы записываются в профиль собаки (dog_titles)
- Публикация результатов (смена статуса → событие)
- Страница результатов выставки (по породам, по рингам)

### Модель данных

Новая таблица: `show_results`, `dog_titles`

show_results:
- show_entry_id → show_entries.id
- grade_id → grades.id (оценка)
- placement (1, 2, 3, 4)
- titles_cache (JSONB — **кэш** для быстрого отображения, source of truth — `dog_titles`)
- critique (TEXT — описание от судьи)
- is_best_of_breed, is_best_of_group, is_best_in_show
- judge_id → users.id

dog_titles (**единственный источник истины о титулах**):
- dog_id → dogs.id
- title_id → titles.id
- show_id → shows.id
- judge_id → users.id
- date_earned

> При вводе результата: INSERT в `show_results` + INSERT в `dog_titles` + обновление `titles_cache` — **всё в одной транзакции**. При редактировании — обновляются оба места атомарно.

### API эндпоинты

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| POST | `/shows/{id}/results` | Ввести результат (оценка + место) | Judge / Organizer |
| PUT | `/shows/{id}/results/{rid}` | Скорректировать результат | Judge / Organizer |
| GET | `/shows/{id}/results` | Все результаты выставки | Public |
| GET | `/shows/{id}/results/by-breed/{breed_id}` | Результаты по породе | Public |
| GET | `/shows/{id}/results/by-ring/{ring_id}` | Результаты ринга | Public |
| POST | `/shows/{id}/results/best-of-breed` | Определить ЛПП | Organizer |
| POST | `/shows/{id}/results/best-in-group` | Определить BIG | Organizer |
| POST | `/shows/{id}/results/best-in-show` | Определить BIS | Organizer |
| POST | `/shows/{id}/publish` | Опубликовать результаты | Organizer |
| GET | `/dogs/{id}/titles` | Все титулы собаки | Public |

### Логика присвоения титулов (show_rules.py)

**В ринге (по классам):**
```
1. Судья выставляет оценки и расстановку (1-4 место)
2. 1-е место с оценкой "отлично" → CW (Победитель класса)
3. CW в промежуточном/открытом/рабочем/чемпионов → автоматически CAC
4. 2-е место с "отлично" в тех же классах → R.CAC
5. CW юниоров → ЮСАС (юный кандидат в чемпионы)
```

**Лучшие кобель/сука породы:**
```
1. Сравнение CW из промежуточного, открытого, рабочего, чемпионов
2. Лучший кобель → ЛК
3. Лучшая сука → ЛС
4. На CACIB выставках: ЛК → CACIB, второй → R.CACIB
```

**Best of Breed (ЛПП):**
```
Сравнение: ЛК vs ЛС vs лучший юниор vs лучший ветеран
Победитель → BOB (ЛПП)
```

**Best in Group (BIG):**
```
Сравнение всех BOB в группе FCI → BIG
```

**Best in Show (BIS):**
```
Сравнение всех BIG (10 групп) → BIS
Отдельно: BIS-Puppy, BIS-Junior, BIS-Veteran
```

### Ключевые концепции

- **Бизнес-правила как код** — правила РКФ кодируются в show_rules.py
- **JSONB как кэш** — titles_cache в show_results для быстрого отображения; `dog_titles` — source of truth
- **Транзакции** — присвоение титула: вставка в show_results + dog_titles + обновление кэша атомарно
- **Event-driven** — публикация результатов генерирует событие для уведомлений (этап 9)

### SQL-фокус

| Что изучаем | Как |
|-------------|-----|
| JSONB (Raw SQL) | `SELECT * FROM show_results WHERE titles @> '[{"code": "CAC"}]'` |
| Complex aggregation (Raw SQL) | Топ-10 собак по количеству CAC: `GROUP BY dog_id ORDER BY COUNT(*) DESC` |
| Multi-table JOIN | results + entries + dogs + breeds + grades + judges |
| INSERT ... RETURNING | Вставка результата с возвратом id |
| Transaction block | Атомарное присвоение оценки + титула + обновление профиля |
| Window functions | RANK() OVER для расстановки в ринге |

### Как проверить

1. `PUT /shows/{id}/status` → in_progress
2. `POST /shows/{id}/results` — ввести оценку "отлично", место 1 → система присваивает CW + CAC
3. `POST /shows/{id}/results/best-of-breed` — определение ЛПП
4. `POST /shows/{id}/results/best-in-show` — определение BIS
5. `POST /shows/{id}/publish` — публикация результатов
6. `GET /dogs/{id}/titles` — титулы появились в профиле собаки
7. `GET /shows/{id}/results/by-breed/5` — результаты по породе
