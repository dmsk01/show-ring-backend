"""
DLX/DLQ helper — общая dead-letter инфраструктура для workflow-очередей.

bug_239 audit 2026-05-28: раньше nack(requeue=False) (или TTL/length-
limit) тихо удалял сообщения — они не попадали ни в одну очередь, и
видимости в malformed payload'ы / истёкшие сообщения у оператора не
было. Сейчас все durable workflow-очереди (document_task, email_tasks,
ad_events, tasks, showtail.events.dispatcher и т.п.) объявляются с
аргументами `x-dead-letter-exchange` + `x-dead-letter-routing-key` —
RabbitMQ автоматически переотправляет "мёртвые" сообщения в общий
DLX, откуда они оседают в DLQ.

Архитектура:

    [producer] → [workflow_queue] ──(nack/ttl/maxlen)──→ [DLX (direct)]
                                                         │
                                                         │ rk=workflow_queue
                                                         ▼
                                                       [DLQ]

Один общий DLX и одна общая DLQ — routing_key=имя исходной очереди
сохраняется (см. x-dead-letter-routing-key), чтобы оператор мог
дифференцировать источники при разборе. Альтернатива «per-queue DLX»
не выбрана: даёт N×2 boilerplate ради сомнительного выигрыша
(имена-то можно прочитать из самого сообщения).

Что делать с DLQ: разбирать вручную или джобом. На этапе 15 имеет
смысл добавить ops-алерт «DLQ growing» через RabbitMQ Management API.

ВАЖНО — миграция существующих очередей: RabbitMQ кидает
PRECONDITION_FAILED при повторном declare с инвалидно отличающимися
аргументами. На первом deploy после этого фикса необходимо удалить
старые workflow-очереди (или применить policy через rabbitmqctl
set_policy) — иначе воркер не стартанёт. На свежем кластере / dev
проблемы нет.
"""

from __future__ import annotations

import aio_pika


# Имя DLX и DLQ. Direct exchange — routing_key должен совпадать с
# x-dead-letter-routing-key очереди-источника.
DLX_EXCHANGE_NAME = "dlx"
DLQ_NAME = "dlq"


async def declare_workflow_queue(
    channel: aio_pika.abc.AbstractChannel,
    queue_name: str,
) -> aio_pika.abc.AbstractQueue:
    """
    Объявляет durable workflow-очередь с привязкой к общему DLX.

    Идемпотентна: повторный вызов с теми же аргументами безопасен.
    DLX и DLQ создаются по требованию — не нужно отдельно их
    bootstrap'ить, любой первый воркер их поднимет. Аргументы
    очереди-источника:
    - x-dead-letter-exchange = DLX_EXCHANGE_NAME (куда переотправлять)
    - x-dead-letter-routing-key = queue_name (как пометить в DLX)

    Returns: declared queue (передавать в consume()).
    """
    # 1. DLX (direct) — durable, чтобы пережил рестарт брокера.
    dlx = await channel.declare_exchange(
        DLX_EXCHANGE_NAME,
        aio_pika.ExchangeType.DIRECT,
        durable=True,
    )

    # 2. DLQ — durable, без message-ttl: пусть оператор сам решает,
    # когда чистить. Длинная DLQ — диагностика плохого деплоя.
    dlq = await channel.declare_queue(DLQ_NAME, durable=True)

    # 3. Bind DLQ ко всем routing_key через сам queue_name. Каждая
    # новая workflow-очередь добавляет свой bind. Идемпотентно.
    await dlq.bind(dlx, routing_key=queue_name)

    # 4. Сама workflow-очередь с DLX-аргументами.
    queue = await channel.declare_queue(
        queue_name,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DLX_EXCHANGE_NAME,
            "x-dead-letter-routing-key": queue_name,
        },
    )
    return queue
