"""Persist signed application sessions in the database."""

from alembic import op
import sqlalchemy as sa


revision = "0004_persistent_login_sessions"
down_revision = "0003_rls_and_encryption_key_check"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "teacher_sessions" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "teacher_sessions",
            sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
            sa.Column("teacher_id", sa.Uuid(as_uuid=False), sa.ForeignKey("teachers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("session_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
    existing_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("teacher_sessions")}
    if "ix_teacher_sessions_teacher_id" not in existing_indexes:
        op.create_index("ix_teacher_sessions_teacher_id", "teacher_sessions", ["teacher_id"])
    if "ix_teacher_sessions_expires_at" not in existing_indexes:
        op.create_index("ix_teacher_sessions_expires_at", "teacher_sessions", ["expires_at"])
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE public.teacher_sessions ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE public.teacher_sessions FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text("REVOKE ALL ON TABLE public.teacher_sessions FROM PUBLIC"))
        op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                    REVOKE ALL ON TABLE public.teacher_sessions FROM anon;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                    REVOKE ALL ON TABLE public.teacher_sessions FROM authenticated;
                END IF;
            END
            $$;
        """))


def downgrade():
    bind = op.get_bind()
    if "teacher_sessions" in sa.inspect(bind).get_table_names():
        op.drop_table("teacher_sessions")
