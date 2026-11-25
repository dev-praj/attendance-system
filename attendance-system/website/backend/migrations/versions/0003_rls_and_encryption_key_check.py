"""Enable RLS without public policies and add query indexes."""

from alembic import op
import sqlalchemy as sa


revision = "0003_rls_and_encryption_key_check"
down_revision = "0002_face_pose_measurements"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    tables = sa.inspect(bind).get_table_names(schema="public")
    for table in tables:
        quoted = bind.dialect.identifier_preparer.quote(table)
        op.execute(sa.text(f"ALTER TABLE public.{quoted} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE public.{quoted} FORCE ROW LEVEL SECURITY"))

    op.execute(sa.text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC"))
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM authenticated';
            END IF;
        END
        $$;
    """))

    indexes = (
        ("ix_attendance_events_student_timestamp", "attendance_events", ["student_id", "timestamp"]),
        ("ix_attendance_records_session_student", "attendance_records", ["session_id", "student_id"]),
        ("ix_class_sessions_class_date", "class_sessions", ["class_id", "session_date"]),
    )
    existing = {
        index["name"]
        for table in ("attendance_events", "attendance_records", "class_sessions")
        for index in sa.inspect(bind).get_indexes(table, schema="public")
    }
    for name, table, columns in indexes:
        if name not in existing:
            op.create_index(name, table, columns, schema="public")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in ("attendance_events", "attendance_records", "class_sessions"):
        for name in (
            "ix_attendance_events_student_timestamp",
            "ix_attendance_records_session_student",
            "ix_class_sessions_class_date",
        ):
            if name in {item["name"] for item in sa.inspect(bind).get_indexes(table, schema="public")}:
                op.drop_index(name, table_name=table, schema="public")
    # RLS and revoked grants are deliberately not undone automatically.
