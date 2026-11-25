"""Move encrypted face records to the encrypted file vault, then remove biometric tables."""

import numpy as np
from alembic import op
import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import sessionmaker


revision = "0006_move_biometrics_out_of_database"
down_revision = "0005_persistent_login_rate_limits"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    has_templates = "face_embeddings" in tables
    has_sessions = "enrollment_sessions" in tables
    if has_templates or has_sessions:
        from app import models
        from app.face.store import face_store
        from app.settings import settings

        cipher = Fernet(settings.encryption_key.encode("ascii"))
        factory = sessionmaker(bind=bind)
        with factory() as db:
            templates = db.query(models.FaceEmbedding).all() if has_templates else []
            sessions = db.query(models.EnrollmentSession).all() if has_sessions else []
            for row in templates:
                try:
                    vector = np.frombuffer(cipher.decrypt(row.encrypted_vector), dtype=np.float32)
                except (InvalidToken, ValueError) as error:
                    raise RuntimeError(
                        "A stored face template could not be decrypted; migration stopped before dropping biometric tables."
                    ) from error
                if vector.shape != (128,) or not np.isfinite(vector).all():
                    raise RuntimeError(
                        "A stored face template is invalid; migration stopped before dropping biometric tables."
                    )
            face_store.import_legacy(templates, sessions)
            face_store.verify()

    if has_templates:
        op.drop_table("face_embeddings")
    if has_sessions:
        op.drop_table("enrollment_sessions")


def downgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "face_embeddings" not in existing:
        op.create_table(
            "face_embeddings",
            sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
            sa.Column("student_id", sa.Uuid(as_uuid=False), sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False),
            sa.Column("enrollment_session_id", sa.Uuid(as_uuid=False), nullable=True),
            sa.Column("pose", sa.String(length=24), nullable=False),
            sa.Column("encrypted_vector", sa.LargeBinary(), nullable=False),
            sa.Column("model_version", sa.String(length=32), nullable=False),
            sa.Column("quality", sa.Float(), nullable=True),
            sa.Column("yaw", sa.Float(), nullable=True),
            sa.Column("pitch", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "enrollment_sessions" not in existing:
        op.create_table(
            "enrollment_sessions",
            sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
            sa.Column("teacher_id", sa.Uuid(as_uuid=False), sa.ForeignKey("teachers.id"), nullable=False),
            sa.Column("student_id", sa.Uuid(as_uuid=False), sa.ForeignKey("students.id"), nullable=False),
            sa.Column("state_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed", sa.Boolean(), nullable=False),
        )
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE public.face_embeddings ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE public.enrollment_sessions ENABLE ROW LEVEL SECURITY"))
