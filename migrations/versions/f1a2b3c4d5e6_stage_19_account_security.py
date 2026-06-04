"""stage_19_account_security

Добавляет users.pending_email (смена email через подтверждение) и
таблицу security_audit_logs (аудит чувствительных операций).

Revision ID: f1a2b3c4d5e6
Revises: e4b8c1f6a2d9
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e4b8c1f6a2d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column('pending_email', sa.String(length=255), nullable=True),
    )
    op.create_table(
        'security_audit_logs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_security_audit_logs_action'),
        'security_audit_logs', ['action'], unique=False,
    )
    op.create_index(
        op.f('ix_security_audit_logs_user_id'),
        'security_audit_logs', ['user_id'], unique=False,
    )
    op.create_index(
        op.f('ix_security_audit_logs_created_at'),
        'security_audit_logs', ['created_at'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_security_audit_logs_created_at'),
        table_name='security_audit_logs',
    )
    op.drop_index(
        op.f('ix_security_audit_logs_user_id'),
        table_name='security_audit_logs',
    )
    op.drop_index(
        op.f('ix_security_audit_logs_action'),
        table_name='security_audit_logs',
    )
    op.drop_table('security_audit_logs')
    op.drop_column('users', 'pending_email')
