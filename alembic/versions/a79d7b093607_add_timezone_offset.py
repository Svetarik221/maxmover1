"""add_timezone_offset

Revision ID: a79d7b093607
Revises: 51fcce37c11c
Create Date: 2026-04-02 19:44:54.039089
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a79d7b093607'
down_revision: Union[str, None] = '51fcce37c11c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('timezone_offset', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'timezone_offset')
