"""add chargebacks

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06

拒付表：渠道争议单号唯一保证重复通知幂等；opened→won/lost 处置状态机。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from shop_common.db import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'chargebacks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('payment_id', sa.String(length=36), nullable=False),
        sa.Column('provider_case_id', sa.String(length=64), nullable=False),
        sa.Column('amount_cents', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('opened_at', UTCDateTime(), nullable=False),
        sa.Column('closed_at', UTCDateTime(), nullable=True),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chargebacks_tenant_id'), 'chargebacks', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_chargebacks_payment_id'), 'chargebacks', ['payment_id'], unique=False)
    op.create_index(op.f('ix_chargebacks_provider_case_id'), 'chargebacks', ['provider_case_id'], unique=True)
    op.create_index(op.f('ix_chargebacks_status'), 'chargebacks', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_chargebacks_status'), table_name='chargebacks')
    op.drop_index(op.f('ix_chargebacks_provider_case_id'), table_name='chargebacks')
    op.drop_index(op.f('ix_chargebacks_payment_id'), table_name='chargebacks')
    op.drop_index(op.f('ix_chargebacks_tenant_id'), table_name='chargebacks')
    op.drop_table('chargebacks')
