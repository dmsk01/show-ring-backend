"""Unit: Actor — кто выполняет операцию (ревью 2026-10-06, BE-33)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.policies import Actor


def _user(*roles: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        roles=[SimpleNamespace(role=SimpleNamespace(value=r)) for r in roles],
    )


def test_of_takes_id_and_roles_from_user():
    user = _user("breeder", "judge")
    actor = Actor.of(user)
    assert actor.user_id == user.id
    assert actor.roles == frozenset({"breeder", "judge"})
    assert not actor.is_admin


def test_admin_passes_any_role_check():
    admin = Actor.of(_user("admin"))
    assert admin.is_admin
    assert admin.has_role("operator")


def test_has_role_any_of():
    actor = Actor.of(_user("organizer"))
    assert actor.has_role("judge", "organizer")
    assert not actor.has_role("operator")


def test_owns():
    actor = Actor(uuid.uuid4())
    assert actor.owns(actor.user_id)
    assert not actor.owns(uuid.uuid4())
    assert not actor.owns(None)
