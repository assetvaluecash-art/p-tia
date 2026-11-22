import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.create_table(
        "payments",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("account_id", sa.String(12), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("reference", sa.String(18), nullable=True),
        sa.Column("disputed", sa.Boolean, nullable=False),
    )


def downgrade():
    op.drop_table("payments")
