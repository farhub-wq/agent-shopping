"""add agent tables

Revision ID: 0001
Revises:
Create Date: 2026-11-19

harness 引入关系库：模型提供商 / 会话 / 消息 / 用量四张表。
密钥 Fernet 密文落库；会话与消息按 user_id + tenant 隔离；用量按用户/时间聚合。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from shop_common.db import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'model_providers',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('owner_user_id', sa.String(length=36), nullable=True),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('provider_type', sa.String(length=24), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('base_url', sa.String(length=256), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('api_key_enc', sa.String(length=512), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('updated_at', UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_model_providers_owner_user_id'), 'model_providers', ['owner_user_id'], unique=False)
    op.create_index(op.f('ix_model_providers_tenant_id'), 'model_providers', ['tenant_id'], unique=False)

    op.create_table(
        'conversations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('skill', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('last_message_at', UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_conversations_user_id'), 'conversations', ['user_id'], unique=False)
    op.create_index(op.f('ix_conversations_tenant_id'), 'conversations', ['tenant_id'], unique=False)

    op.create_table(
        'messages',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('tool_calls_json', sa.Text(), nullable=True),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_messages_conversation_id'), 'messages', ['conversation_id'], unique=False)

    op.create_table(
        'usage_records',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('provider_id', sa.String(length=36), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=True),
        sa.Column('run_id', sa.String(length=36), nullable=True),
        sa.Column('prompt_tokens', sa.BigInteger(), nullable=False),
        sa.Column('completion_tokens', sa.BigInteger(), nullable=False),
        sa.Column('total_tokens', sa.BigInteger(), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_usage_records_user_id'), 'usage_records', ['user_id'], unique=False)
    op.create_index(op.f('ix_usage_records_tenant_id'), 'usage_records', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_usage_records_created_at'), 'usage_records', ['created_at'], unique=False)
    # 监控页聚合：按用户/时间、按 provider/时间两路查询走复合索引，避免全表扫描。
    op.create_index('ix_usage_records_user_created', 'usage_records', ['user_id', 'created_at'], unique=False)
    op.create_index('ix_usage_records_provider_created', 'usage_records', ['provider_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_usage_records_provider_created', table_name='usage_records')
    op.drop_index('ix_usage_records_user_created', table_name='usage_records')
    op.drop_index(op.f('ix_usage_records_created_at'), table_name='usage_records')
    op.drop_index(op.f('ix_usage_records_tenant_id'), table_name='usage_records')
    op.drop_index(op.f('ix_usage_records_user_id'), table_name='usage_records')
    op.drop_table('usage_records')

    op.drop_index(op.f('ix_messages_conversation_id'), table_name='messages')
    op.drop_table('messages')

    op.drop_index(op.f('ix_conversations_tenant_id'), table_name='conversations')
    op.drop_index(op.f('ix_conversations_user_id'), table_name='conversations')
    op.drop_table('conversations')

    op.drop_index(op.f('ix_model_providers_tenant_id'), table_name='model_providers')
    op.drop_index(op.f('ix_model_providers_owner_user_id'), table_name='model_providers')
    op.drop_table('model_providers')
