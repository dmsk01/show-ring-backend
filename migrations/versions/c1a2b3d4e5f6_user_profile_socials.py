"""user_profile_socials

Revision ID: c1a2b3d4e5f6
Revises: b7e2f4a9c1d3
Create Date: 2026-06-15 12:00:00.000000

Ссылки на соцсети в профиле пользователя (запрос фронта: страница
/dashboard/profile/socials, до этого стояла заглушка «Раздел появится
после поддержки на сервере»).

Набор сетей — Instagram, Facebook, VK, Telegram — зеркалит поля формы
на фронте (src/sections/profile/profile-placeholder.tsx). Кладём в
существующую таблицу user_profiles (1:1 с users), а не в отдельную:
фиксированный небольшой набор колонок проще и согласован с тем, как там
уже лежат ФИО/страна.

Все колонки nullable, бэкофилл не нужен: NULL = ссылка не указана.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1a2b3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'b7e2f4a9c1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = ('instagram', 'facebook', 'vk', 'telegram')


def upgrade() -> None:
    """Upgrade schema."""
    for col in _COLUMNS:
        op.add_column(
            'user_profiles', sa.Column(col, sa.String(255), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    for col in reversed(_COLUMNS):
        op.drop_column('user_profiles', col)
