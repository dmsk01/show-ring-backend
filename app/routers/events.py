from fastapi import APIRouter, Query
from app.schemas.event import Event
from app.services.rabbit import rabbit_service
from app.config import settings

router = APIRouter(prefix="/events", tags=["events"])


@router.post("/broadcast", summary="Отправить произвольное событие всем подписчикам")
async def publish_event(event: Event):
    await rabbit_service.publish_to_exchange(
        exchange_name=settings.exchange_events, message=event.model_dump_json()
    )
    return {"status": "broadcasted", "event_type": event.event_type}


@router.post("/cache/invalidate", summary="Инвалидация кеша")
async def cache_invalidate(
    entity_id: int = Query(gt=0),
    entity_type: str = Query(max_length=50),
):
    event = Event(
        event_type="cache.invalidate",
        data={"entity_type": entity_type, "entity_id": entity_id},
        source="admin_api",
    )

    await rabbit_service.publish_to_exchange(
        exchange_name=settings.exchange_events, message=event.model_dump_json()
    )
    return {"status": "broadcasted", "event_type": event.event_type}
