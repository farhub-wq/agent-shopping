"""add conversation context / summary / archive

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24

P2 长对话治理：
- conversations 增加父子链、累计 token、上下文状态与交接摘要指针；
- messages 增加会话内序号、token 数与软归档标记；
- 新增 conversation_summaries（拆分/回灌摘要）与 message_archives（gzip 压缩归档）。

每个 DDL 前置存在性判断：SQLite / MySQL 的 DDL 都可能逐条提交，
中途失败会留下「部分已应用」状态，幂等迁移才能安全重跑。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from shop_common.db import UTCDateTime
from sqlalchemy.dialects.mysql import LONGTEXT

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table: str) -> set[str]:
    return {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _add_columns(table: str, columns: dict[str, sa.Column]) -> None:
    existing = _columns(table)
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table, column)


def _create_indexes(table: str, indexes: dict[str, list[str]]) -> None:
    existing = _indexes(table)
    for name, columns in indexes.items():
        if name not in existing:
            op.create_index(name, table, columns, unique=False)


def _drop_columns(table: str, names: list[str]) -> None:
    existing = _columns(table)
    for name in names:
        if name in existing:
            op.drop_column(table, name)


def _drop_indexes(table: str, names: list[str]) -> None:
    existing = _indexes(table)
    for name in names:
        if name in existing:
            op.drop_index(name, table_name=table)


def upgrade() -> None:
    _add_columns(
        'conversations',
        {
            'root_id': sa.Column('root_id', sa.String(length=36), nullable=True),
            'parent_id': sa.Column('parent_id', sa.String(length=36), nullable=True),
            'token_count': sa.Column(
                'token_count', sa.BigInteger(), nullable=False, server_default=sa.text('0')
            ),
            'context_state': sa.Column(
                'context_state', sa.String(length=16), nullable=False, server_default='ok'
            ),
            'summary_id': sa.Column('summary_id', sa.String(length=36), nullable=True),
        },
    )
    _create_indexes(
        'conversations',
        {
            'ix_conversations_root_id': ['root_id'],
            'ix_conversations_parent_id': ['parent_id'],
        },
    )
    # 存量会话没有父链时把自己作为链路起点，保证后续拆分能继承 root_id。
    op.execute('UPDATE conversations SET root_id = id WHERE root_id IS NULL')

    _add_columns(
        'messages',
        {
            'seq': sa.Column('seq', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
            'token_count': sa.Column(
                'token_count', sa.Integer(), nullable=False, server_default=sa.text('0')
            ),
            'is_archived': sa.Column(
                'is_archived', sa.Boolean(), nullable=False, server_default=sa.text('0')
            ),
            'archive_id': sa.Column('archive_id', sa.String(length=36), nullable=True),
        },
    )
    _create_indexes(
        'messages',
        {'ix_messages_conversation_archived': ['conversation_id', 'is_archived', 'created_at']},
    )

    tables = _tables()
    if 'conversation_summaries' not in tables:
        op.create_table(
            'conversation_summaries',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('conversation_id', sa.String(length=36), nullable=False),
            sa.Column('user_id', sa.String(length=36), nullable=False),
            sa.Column('tenant_id', sa.String(length=36), nullable=False),
            sa.Column('text', sa.Text(), nullable=False),
            sa.Column('pinned_json', sa.Text(), nullable=False),
            sa.Column('summary_level', sa.Integer(), nullable=False),
            sa.Column('source', sa.String(length=16), nullable=False),
            sa.Column('created_at', UTCDateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
    _create_indexes(
        'conversation_summaries',
        {
            'ix_conversation_summaries_conversation_id': ['conversation_id'],
            'ix_conversation_summaries_user_id': ['user_id'],
            'ix_conversation_summaries_tenant_id': ['tenant_id'],
            'ix_conversation_summaries_user_conversation': [
                'user_id',
                'conversation_id',
                'created_at',
            ],
        },
    )

    if 'message_archives' not in tables:
        op.create_table(
            'message_archives',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('conversation_id', sa.String(length=36), nullable=False),
            sa.Column('user_id', sa.String(length=36), nullable=False),
            sa.Column('tenant_id', sa.String(length=36), nullable=False),
            sa.Column('message_count', sa.Integer(), nullable=False),
            sa.Column('first_message_at', UTCDateTime(), nullable=True),
            sa.Column('last_message_at', UTCDateTime(), nullable=True),
            sa.Column('encoding', sa.String(length=16), nullable=False),
            # MySQL 用 LONGTEXT 容纳整批归档；SQLite 等方言回落 TEXT。
            sa.Column('payload', sa.Text().with_variant(LONGTEXT(), 'mysql'), nullable=False),
            sa.Column('created_at', UTCDateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
    _create_indexes(
        'message_archives',
        {
            'ix_message_archives_conversation_id': ['conversation_id'],
            'ix_message_archives_user_id': ['user_id'],
            'ix_message_archives_tenant_id': ['tenant_id'],
        },
    )


def downgrade() -> None:
    tables = _tables()
    if 'message_archives' in tables:
        _drop_indexes(
            'message_archives',
            [
                'ix_message_archives_tenant_id',
                'ix_message_archives_user_id',
                'ix_message_archives_conversation_id',
            ],
        )
        op.drop_table('message_archives')

    if 'conversation_summaries' in tables:
        _drop_indexes(
            'conversation_summaries',
            [
                'ix_conversation_summaries_user_conversation',
                'ix_conversation_summaries_tenant_id',
                'ix_conversation_summaries_user_id',
                'ix_conversation_summaries_conversation_id',
            ],
        )
        op.drop_table('conversation_summaries')

    _drop_indexes('messages', ['ix_messages_conversation_archived'])
    _drop_columns('messages', ['archive_id', 'is_archived', 'token_count', 'seq'])

    _drop_indexes(
        'conversations', ['ix_conversations_parent_id', 'ix_conversations_root_id']
    )
    _drop_columns(
        'conversations',
        ['summary_id', 'context_state', 'token_count', 'parent_id', 'root_id'],
    )
