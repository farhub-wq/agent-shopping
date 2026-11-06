"""add MFA columns to users

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06 21:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 管理员 TOTP：secret 加密落库，enabled 控制是否要求二次验证，last_totp_step 防重放
    op.add_column('users', sa.Column('totp_secret', sa.String(length=512), nullable=True))
    op.add_column('users', sa.Column('totp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('users', sa.Column('last_totp_step', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_totp_step')
    op.drop_column('users', 'totp_enabled')
    op.drop_column('users', 'totp_secret')
