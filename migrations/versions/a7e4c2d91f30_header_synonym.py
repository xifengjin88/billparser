"""header_synonym: column headers learned for canonical fields

Revision ID: a7e4c2d91f30
Revises: 3c1d7e2a9b40
Create Date: 2026-10-27 17:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = 'a7e4c2d91f30'
down_revision: Union[str, Sequence[str], None] = '3c1d7e2a9b40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('header_synonym',
    sa.Column('header_norm', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('field', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('rejected', sa.Boolean(), nullable=False),
    sa.Column('created', sa.Float(), nullable=False),
    sa.Column('updated', sa.Float(), nullable=False),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.PrimaryKeyConstraint('header_norm', 'field')
    )
    op.create_index(op.f('ix_header_synonym_updated'), 'header_synonym', ['updated'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_header_synonym_updated'), table_name='header_synonym')
    op.drop_table('header_synonym')
