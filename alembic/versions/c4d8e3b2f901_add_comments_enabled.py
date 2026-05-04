"""add_comments_enabled

Revision ID: c4d8e3b2f901
Revises: b3c7f2a1d890
Create Date: 2026-04-04 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c4d8e3b2f901'
down_revision: Union[str, None] = 'b3c7f2a1d890'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('channels', sa.Column('comments_enabled', sa.Boolean(), nullable=False, server_default='true'))


def downgrade() -> None:
    op.drop_column('channels', 'comments_enabled')
