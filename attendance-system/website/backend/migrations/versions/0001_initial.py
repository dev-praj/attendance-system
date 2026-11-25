"""Initial attendance schema."""

from alembic import op

from app.database import Base
import app.models  # noqa: F401


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    # Keep destructive table removal explicit rather than dropping user data by default.
    pass
