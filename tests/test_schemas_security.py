"""Schema-инварианты — что НЕ должно утекать наружу через сериализацию."""

from app.schemas.user import PublicUserResponse, UserResponse


def test_public_user_response_hides_email():
    """Публичная схема не должна содержать email — иначе любой неавторизованный
    может собирать email'ы перебором UUID через GET /users/{id}."""
    fields = set(PublicUserResponse.model_fields.keys())
    assert "email" not in fields
    assert "is_email_verified" not in fields
    # минимально публично-безопасные поля
    assert {"id", "is_active", "roles", "created_at"}.issubset(fields)


def test_private_user_response_exposes_email():
    """UserResponse (для /users/me) — наоборот, должен содержать email."""
    fields = set(UserResponse.model_fields.keys())
    assert "email" in fields
    assert "is_email_verified" in fields
