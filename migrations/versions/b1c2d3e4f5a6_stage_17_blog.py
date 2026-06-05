"""stage_17_blog

Revision ID: b1c2d3e4f5a6
Revises: a7d4e9c1f3b8
Create Date: 2026-06-05 00:00:00.000000

Этап 17 (блог): таблица posts + PG-enum postpublish.

posts хранит статьи блога. slug — UNIQUE+index (публичный URL и ключ
lookup'а). tags/meta_keywords — PG ARRAY(String) одной колонкой (проще
join-таблицы, хватает под поиск тега `= ANY(tags)`). publish — enum
published/draft, default draft (черновик не виден публично). author_id —
FK→users SET NULL (пост переживает удаление автора). total_* — счётчики
с дефолтом 0.

downgrade удаляет таблицу и enum-тип (в отличие от ALTER TYPE ADD VALUE,
DROP TABLE откатывается чисто).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a7d4e9c1f3b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Создаём enum-тип явно (checkfirst — идемпотентно). create_table ниже
    # ссылается на него уже как на существующий тип.
    postpublish = postgresql.ENUM('published', 'draft', name='postpublish')
    postpublish.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'posts',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('slug', sa.String(length=320), nullable=False),
        sa.Column(
            'description', sa.Text(), server_default='', nullable=False
        ),
        sa.Column('content', sa.Text(), server_default='', nullable=False),
        sa.Column('cover_url', sa.String(length=1024), nullable=True),
        sa.Column(
            'tags',
            postgresql.ARRAY(sa.String()),
            server_default='{}',
            nullable=False,
        ),
        sa.Column(
            'meta_keywords',
            postgresql.ARRAY(sa.String()),
            server_default='{}',
            nullable=False,
        ),
        sa.Column('meta_title', sa.String(length=300), nullable=True),
        sa.Column('meta_description', sa.Text(), nullable=True),
        sa.Column(
            'publish',
            # create_type=False — тип уже создан выше, не пытаемся снова.
            postgresql.ENUM(
                'published', 'draft', name='postpublish', create_type=False
            ),
            server_default='draft',
            nullable=False,
        ),
        sa.Column('author_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            'total_views', sa.Integer(), server_default='0', nullable=False
        ),
        sa.Column(
            'total_shares', sa.Integer(), server_default='0', nullable=False
        ),
        sa.Column(
            'total_comments', sa.Integer(), server_default='0', nullable=False
        ),
        sa.Column(
            'total_favorites',
            sa.Integer(),
            server_default='0',
            nullable=False,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['author_id'], ['users.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_posts_slug', 'posts', ['slug'], unique=True)
    op.create_index('ix_posts_publish', 'posts', ['publish'])
    op.create_index('ix_posts_author_id', 'posts', ['author_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_posts_author_id', table_name='posts')
    op.drop_index('ix_posts_publish', table_name='posts')
    op.drop_index('ix_posts_slug', table_name='posts')
    op.drop_table('posts')
    postgresql.ENUM(name='postpublish').drop(op.get_bind(), checkfirst=True)
