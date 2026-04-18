from pydantic import BaseModel


class BookBase(BaseModel):
    title: str
    author: str


class BookCreate(BookBase):
    pass


class BookResponse(BookBase):
    id: int


class BookWithTask(BaseModel):
    book: BookResponse
    task_id: str
