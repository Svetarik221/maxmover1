"""add_comments

Revision ID: b3c7f2a1d890
Revises: a79d7b093607
Create Date: 2026-04-04 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b3c7f2a1d890'
down_revision: Union[str, None] = 'a79d7b093607'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('comments',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('max_message_id', sa.String(length=255), nullable=False),
        sa.Column('max_chat_id', sa.String(length=255), nullable=False),
        sa.Column('max_user_id', sa.BigInteger(), nullable=False),
        sa.Column('user_name', sa.String(length=255), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_comments_max_message_id', 'comments', ['max_message_id'])


def downgrade() -> None:
    op.drop_index('ix_comments_max_message_id', table_name='comments')
    op.drop_table('comments')
