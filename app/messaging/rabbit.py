#!/usr/bin/env python
"""
Учебный скрипт «Hello World» для RabbitMQ. Запускается вручную как
песочница; в основном приложении используется app.services.rabbit
с aio-pika.
"""
import logging

import pika

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

connection = pika.BlockingConnection(
    pika.ConnectionParameters(host='localhost'))
channel = connection.channel()

channel.queue_declare(queue='hello')

channel.basic_publish(exchange='', routing_key='hello', body='Hello World!')
# logger вместо print: print не попадает в JSON-логи прод-инстанса
# и теряется в Docker'е без stdout-захвата.
logger.info("Sent 'Hello World!'")
connection.close()
