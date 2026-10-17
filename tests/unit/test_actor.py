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


def test_operator_rules():
    from app.policies import can_access_ticket, is_operator

    author, operator, stranger = Actor(uuid.uuid4()), Actor.of(_user("operator")), Actor(uuid.uuid4())
    admin = Actor.of(_user("admin"))
    ticket = SimpleNamespace(user_id=author.user_id, assigned_to_id=None)
    assert is_operator(operator) and is_operator(admin)
    assert not is_operator(author)
    assert can_access_ticket(ticket, author)
    assert can_access_ticket(ticket, operator)
    assert not can_access_ticket(ticket, stranger)
    assigned = SimpleNamespace(user_id=author.user_id, assigned_to_id=stranger.user_id)
    assert can_access_ticket(assigned, stranger)


def test_is_organizer_of():
    from app.policies import is_organizer_of

    org = Actor(uuid.uuid4())
    assert is_organizer_of(org, org.user_id)
    assert not is_organizer_of(org, uuid.uuid4())
    assert is_organizer_of(Actor.of(_user("admin")), uuid.uuid4())
