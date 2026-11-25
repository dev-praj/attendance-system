"""Store measured head pose alongside each encrypted face template."""

from alembic import op
import sqlalchemy as sa


revision = "0002_face_pose_measurements"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("face_embeddings")}
    if "yaw" not in columns:
        op.add_column("face_embeddings", sa.Column("yaw", sa.Float(), nullable=True))
    if "pitch" not in columns:
        op.add_column("face_embeddings", sa.Column("pitch", sa.Float(), nullable=True))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("face_embeddings")}
    if "pitch" in columns:
        op.drop_column("face_embeddings", "pitch")
    if "yaw" in columns:
        op.drop_column("face_embeddings", "yaw")
