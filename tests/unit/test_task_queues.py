"""
Карта task.type → очередь должна покрывать ВСЕ типы задач.

Регрессия на баг (review 2026-06-01): официальные документы и
process_image выпадали из карты, и requeue_stuck_tasks тихо терял
зависшие задачи (pending навсегда). Тест ловит добавление нового
DocumentKind без записи в карту.
"""

from app.schemas.task import DocumentKind
from app.services.task_queues import (
    DOCUMENT_TASK_QUEUE,
    IMAGE_TASK_QUEUE,
    IMAGE_TASK_TYPE,
    QUEUE_FOR_TASK_TYPE,
)


def test_every_document_kind_has_queue():
    for kind in DocumentKind:
        assert kind.value in QUEUE_FOR_TASK_TYPE, f"нет очереди для {kind}"


def test_all_documents_route_to_document_queue():
    for kind in DocumentKind:
        assert QUEUE_FOR_TASK_TYPE[kind.value] == DOCUMENT_TASK_QUEUE


def test_official_documents_covered():
    # Именно эти типы выпадали из старой карты.
    for kind in (
        DocumentKind.CATALOG_OFFICIAL,
        DocumentKind.DIPLOMA_OFFICIAL,
        DocumentKind.DIPLOMAS_BATCH_OFFICIAL,
        DocumentKind.RING_SHEETS_OFFICIAL,
        DocumentKind.CERTIFICATES_OFFICIAL,
    ):
        assert kind.value in QUEUE_FOR_TASK_TYPE


def test_process_image_routes_to_image_queue():
    assert QUEUE_FOR_TASK_TYPE[IMAGE_TASK_TYPE] == IMAGE_TASK_QUEUE
