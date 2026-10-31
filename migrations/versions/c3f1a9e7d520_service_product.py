"""service_product: the products and services each provider publishes (the service catalog's code lists)

Revision ID: c3f1a9e7d520
Revises: a7d2e9c41b05
Create Date: 2026-10-31 20:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = 'c3f1a9e7d520'
down_revision: Union[str, Sequence[str], None] = 'a7d2e9c41b05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('service_product',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('product_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('product_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('service_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('service_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('family', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('attributes', sa.JSON(), nullable=True),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('fetched_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'product_id', 'product_name', name='service_product_key')
    )
    op.create_index(op.f('ix_service_product_provider'), 'service_product', ['provider'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_service_product_provider'), table_name='service_product')
    op.drop_table('service_product')
