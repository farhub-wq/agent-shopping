"""add payment user_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06

支付单增加归属用户字段：mock 收银台确认与支付确认按此校验归属，
存量行以空串回填（无归属的旧支付单不可被任何人代付，只能由内部身份处理）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('payments', sa.Column('user_id', sa.String(length=36), nullable=False, server_default=''))
    op.create_index(op.f('ix_payments_user_id'), 'payments', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_payments_user_id'), table_name='payments')
    op.drop_column('payments', 'user_id')
