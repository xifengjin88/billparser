"""job.callback_then: the webhook event to send after the pending one

Revision ID: 3c1d7e2a9b40
Revises: 78fc8de19bf8
Create Date: 2026-10-27 16:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = '3c1d7e2a9b40'
down_revision: Union[str, Sequence[str], None] = '78fc8de19bf8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('job', sa.Column('callback_then', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('job', 'callback_then')
