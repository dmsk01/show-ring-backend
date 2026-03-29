import uuid

from fastapi import APIRouter, HTTPException
from app.services.rabbit import rabbit_service
from app.services.task_storage import task_storage
from app.schemas.task import TaskMessage
from app.schemas.book import BookCreate, BookResponse, BookWithTask

books: list[dict] = [
    {"id": 1, "title": "The Great Gatsby", "author": "F. Scott Fitzgerald"},    
    {"id": 2, "title": "To Kill a Mockingbird", "author": "Harper Lee"},        
    {"id": 3, "title": "1984", "author": "George Orwell"},
]

router = APIRouter(prefix="/books", tags=["books"])

@router.get('/', response_model=list[BookResponse], summary="Получить список книг", description="Возвращает список всех книг.")
def get_books():
    return books

@router.get('/{book_id}/', summary="Получить книгу по ID", description="Возвращает книгу с указанным ID. Если книга не найдена, возвращает ошибку 404.")
def get_book_by_id(book_id: int):
    book = None
    for item in books:
        if item["id"] == book_id:
            book = item

    if book is None:
        raise HTTPException(status_code=404, detail=f"Book with id {book_id} not found")

    return book

@router.post('/', summary="Добавить новую книгу", description="Добавляет новую книгу в список. Требует JSON с полями 'title' и 'author'.")
async def create_book(body: BookCreate):
    new_id = max(book["id"] for book in books) + 1 if books else 1
    new_book = {"id": new_id, "title": body.title, "author": body.author}
    books.append(new_book)
    task_id = str(uuid.uuid4())
    task_storage.create_task(task_id)

    message = TaskMessage(
      task_id=task_id,
      action="process_book",
      payload={"book_id": new_id, "title": body.title, "author": body.author}     
    )

    await rabbit_service.publish("book_task", message.to_json())
    return BookWithTask(
      book=BookResponse(**new_book),
      task_id=task_id
  )

@router.delete('/{book_id}/', status_code=204, summary="Удалить книгу", description="Удаляет книгу из списка по указаннму book_id")
def delete_book(book_id: int):
    for index, item in enumerate(books):
      if item["id"] == book_id:
          books.pop(index)
          return
    raise HTTPException(status_code=404, detail=f"Book with id {book_id} not found")
