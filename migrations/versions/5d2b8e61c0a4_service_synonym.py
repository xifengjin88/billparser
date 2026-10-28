"""service_synonym: service-name synonyms learned by review and admins (was CATALOG_DIR/synonyms.json)

Revision ID: 5d2b8e61c0a4
Revises: a7e4c2d91f30
Create Date: 2026-10-27 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = '5d2b8e61c0a4'
down_revision: Union[str, Sequence[str], None] = 'a7e4c2d91f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('service_synonym',
    sa.Column('scope', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('label', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('service_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created', sa.Float(), nullable=False),
    sa.Column('updated', sa.Float(), nullable=False),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.PrimaryKeyConstraint('scope', 'label')
    )
    op.create_index(op.f('ix_service_synonym_updated'), 'service_synonym', ['updated'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_service_synonym_updated'), table_name='service_synonym')
    op.drop_table('service_synonym')
