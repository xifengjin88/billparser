"""price_product: Azure's meter name, product name and SKU name, and the meter-name index

Revision ID: a7d2e9c41b05
Revises: 5d2b8e61c0a4
Create Date: 2026-10-27 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7d2e9c41b05'
down_revision: Union[str, Sequence[str], None] = '5d2b8e61c0a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. Nullable columns only: the AWS rows are not rewritten."""
    op.add_column('price_product', sa.Column('meter_name', sa.String(), nullable=True))
    op.add_column('price_product', sa.Column('product_name', sa.String(), nullable=True))
    op.add_column('price_product', sa.Column('sku_name', sa.String(), nullable=True))
    op.create_index('price_product_meter', 'price_product', ['provider', 'meter_name', 'region'], unique=False,
                    postgresql_include=['service_code', 'operating_system', 'product_name', 'sku'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('price_product_meter', table_name='price_product')
    op.drop_column('price_product', 'sku_name')
    op.drop_column('price_product', 'product_name')
    op.drop_column('price_product', 'meter_name')
