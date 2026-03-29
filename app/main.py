from fastapi import FastAPI

from contextlib import asynccontextmanager
from app.config import settings
from app.services.rabbit import rabbit_service
from app.routers import books, tasks

@asynccontextmanager
async def lifespan(app: FastAPI):
    await rabbit_service.connect(settings.rabbitmq_url)
    print("Connected to RabbitMQ")
    yield
    await rabbit_service.close()
    print("Disconnected from RabbitMQ")

app = FastAPI(lifespan=lifespan)
app.include_router(tasks.router)
app.include_router(books.router)
