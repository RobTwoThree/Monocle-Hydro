"""add reserve type

Revision ID: 436fca55d46a
Revises: dff614136e01
Create Date: 2017-10-18 04:49:32.331235

"""
import sys
from pathlib import Path
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import expression
monocle_dir = str(Path(__file__).resolve().parents[2])
if monocle_dir not in sys.path:
    sys.path.append(monocle_dir)
from monocle.accounts import Account, RESERVE_TYPE_SLAVE, RESERVE_TYPE_CAPTAIN

# revision identifiers, used by Alembic.
revision = '436fca55d46a'
down_revision = 'dff614136e01'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('accounts', sa.Column('reserve_type', sa.SmallInteger(), nullable=True, default=0))
    op.create_index('ix_accounts_acquisition', 'accounts', ['reserve_type', 'instance', 'hibernated', 'created'], unique=False)

    query = Account.__table__.update().\
            values({'reserve_type':expression.case([
                (Account.level < 30,RESERVE_TYPE_SLAVE),
                ],
                else_=RESERVE_TYPE_CAPTAIN)})
    op.execute(query)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_accounts_acquisition', table_name='accounts')
    op.drop_column('accounts', 'reserve_type')
    # ### end Alembic commands ###
