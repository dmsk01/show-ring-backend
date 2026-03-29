# Чеклист готовности

### Этап 1 ✅
- [x] config.py читает .env
- [x] RabbitMQService подключается и публикует
- [x] POST /tasks/send работает
- [x] Worker получает и выводит сообщения

### Этап 2 ✅
- [x] TaskMessage и TaskStatus модели созданы
- [x] TaskStorage хранит статусы
- [x] POST /books создаёт задачу и отправляет в очередь
- [x] GET /tasks/{id} возвращает статус
- [x] PUT /tasks/{id}/status обновляет статус (защищён ключом)
- [x] Worker обрабатывает book_tasks и обновляет статус через API

### Этап 3
- [ ] Fanout exchange "events" создаётся
- [ ] POST /events/broadcast публикует
- [ ] Несколько воркеров в режиме fanout получают копии
- [ ] POST /events/cache/invalidate работает

### Этап 4
- [ ] Topic exchange "app_events" создаётся
- [ ] Routing keys используются при публикации
- [ ] Воркеры фильтруют по паттернам (book.*, *.created, #)

### Этап 5
- [ ] Vue проект создан
- [ ] CORS настроен
- [ ] Форма создаёт книги
- [ ] TaskMonitor показывает статус с polling
- [ ] BookList обновляется

### Этап 6 (FastStream)
- [ ] Установлен faststream[rabbit]
- [ ] Создан FastStream-воркер с декларативными хендлерами
- [ ] Реализована интеграция FastAPI + FastStream
- [ ] Добавлены Publisher для отправки сообщений
- [ ] Настроено RPC (request-reply паттерн)
- [ ] Добавлено тестирование с TestRabbitBroker

### Этап 7 (DI на практике)
- [ ] Создан app/dependencies.py
- [ ] RabbitDep и TaskStorageDep тип-алиасы определены
- [ ] Роутеры books.py и tasks.py переписаны на Depends()
- [ ] Всё работает как раньше

### Этап 8 (Middleware)
- [ ] Создан app/middleware/ пакет
- [ ] RequestLoggingMiddleware логирует запросы
- [ ] RequestIDMiddleware генерирует X-Request-ID
- [ ] Middleware зарегистрированы в main.py

### Этап 9 (Тестирование)
- [ ] pytest, pytest-asyncio, httpx установлены
- [ ] conftest.py с фикстурами создан
- [ ] Unit-тесты TaskStorage проходят
- [ ] Интеграционные тесты /books и /tasks проходят
- [ ] pytest --cov показывает покрытие

### Этап 10 (Structured Logging)
- [ ] app/logging_config.py создан
- [ ] setup_logging() вызывается в main.py
- [ ] Все print() заменены на logger
- [ ] JSON-формат работает с LOG_JSON=true

### Этап 11 (Reconnection)
- [ ] connect_robust() используется в app/services/rabbit.py
- [ ] connect_robust() используется в worker/main.py
- [ ] reconnect_callbacks логируют переподключение
- [ ] Тест: перезапуск RabbitMQ не ломает приложение

### Этап 12 (Idempotency)
- [ ] BookHandler проверяет task_id перед обработкой
- [ ] Дублирующие сообщения пропускаются с логом
- [ ] Повторная отправка не создаёт дубликат

### Этап 13 (Docker Compose)
- [ ] Dockerfile создан
- [ ] docker-compose.yml с rabbitmq, api, worker
- [ ] .dockerignore настроен
- [ ] docker-compose up запускает весь стек
- [ ] --scale worker=2 распределяет задачи
