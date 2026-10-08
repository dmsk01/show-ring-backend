"""show_entries.dog_id / dog_titles.dog_id: CASCADE → RESTRICT (ревью 2026-10-06, BE-10)

Удаление собаки каскадно стирало её записи, результаты и титулы — в том
числе на завершённых выставках (опубликованный каталог, дипломы,
сертификаты РКФ). Теперь БД не даёт удалить собаку с историей; снятие
записей на ещё открытые выставки делает сервис явно (services/dog.py).

Revision ID: d4e5f6a7b8c1
Revises: c3d4e5f6a7b0
Create Date: 2026-10-06
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d4e5f6a7b8c1"
down_revision: Union[str, None] = "c3d4e5f6a7b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FKS = (
    ("show_entries", "show_entries_dog_id_fkey"),
    ("dog_titles", "dog_titles_dog_id_fkey"),
)


def _recreate(ondelete: str) -> None:
    for table, name in _FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, "dogs", ["dog_id"], ["id"], ondelete=ondelete
        )


def upgrade() -> None:
    _recreate("RESTRICT")


def downgrade() -> None:
    _recreate("CASCADE")
