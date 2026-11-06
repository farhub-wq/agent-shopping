"""drop legacy reservations table

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06

清理历史遗留的 reservations 孤儿表：早期模型用 reservations 表名，
后来改名为 stock_reservations，旧表在存量库里仍存在但已不被模型引用。
条件判断避免在全新库上误报（新库根本没有这张表）。
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
    inspector = sa.inspect(op.get_bind())
    if "reservations" in inspector.get_table_names():
        op.drop_table("reservations")


def downgrade() -> None:
    # 遗留孤儿表无法从模型还原，回滚不重建，保持单向清理语义。
    pass
