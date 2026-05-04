"""add_max_user_id

Revision ID: d5e9f4c3g012
Revises: c4d8e3b2f901
Create Date: 2026-04-04 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd5e9f4c30012'
down_revision: Union[str, None] = 'c4d8e3b2f901'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('max_user_id', sa.BigInteger(), nullable=True))
    op.create_unique_constraint('uq_users_max_user_id', 'users', ['max_user_id'])


def downgrade() -> None:
    op.drop_constraint('uq_users_max_user_id', 'users')
    op.drop_column('users', 'max_user_id')
