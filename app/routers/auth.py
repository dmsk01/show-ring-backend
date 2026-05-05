from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.services.auth import (
    refresh_access_token,
    register_user,
    verify_email,
    login_user,
    logout_user,
)
from app.schemas.user import RefreshRequest, TokenResponse, UserCreate

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    summary="Регистрация пользователя",
    description="Создаёт аккаунт и отправляет письмо с ссылкой для подтверждения email.",
)
async def register(body: UserCreate, db: AsyncSession = Depends(get_db)):
    try:
        await register_user(db, body.email, body.password)
        return {"message": "Проверьте email для подтверждения"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/verify-email",
    summary="Подтверждение email",
    description="Принимает одноразовый токен из письма и активирует email пользователя.",
)
async def verify_user_email(token: str = Query(...), db: AsyncSession = Depends(get_db)):
    try:
        await verify_email(db, token)
        return {"message": "Email подтверждён"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/login",
    summary="Вход в систему",
    description="Проверяет email и пароль, возвращает access token (15 мин) и refresh token (7 дней).",
)
async def login(body: UserCreate, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    try:
        return await login_user(db, body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post(
    "/refresh",
    summary="Обновление access token",
    description="Принимает действующий refresh token, возвращает новый access token.",
)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        access_token = await refresh_access_token(db, body.refresh_token)
        return {"access_token": access_token}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post(
    "/logout",
    summary="Выход из системы",
    description="Отзывает refresh token. После этого обновление access token становится невозможным.",
)
async def logout(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        await logout_user(db, body.refresh_token)
        return {"message": "Успешный выход"}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
