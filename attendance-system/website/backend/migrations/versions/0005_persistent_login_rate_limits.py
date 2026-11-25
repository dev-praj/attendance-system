"""Persist login IP rate-limit windows without storing raw IP addresses."""

from alembic import op
import sqlalchemy as sa


revision = "0005_persistent_login_rate_limits"
down_revision = "0004_persistent_login_sessions"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "login_rate_limits" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "login_rate_limits",
            sa.Column("ip_hash", sa.String(length=64), primary_key=True),
            sa.Column("attempt_times", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE public.login_rate_limits ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE public.login_rate_limits FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text("REVOKE ALL ON TABLE public.login_rate_limits FROM PUBLIC"))
        op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                    EXECUTE 'REVOKE ALL ON TABLE public.login_rate_limits FROM anon';
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                    EXECUTE 'REVOKE ALL ON TABLE public.login_rate_limits FROM authenticated';
                END IF;
            END
            $$;
        """))


def downgrade():
    bind = op.get_bind()
    if "login_rate_limits" in sa.inspect(bind).get_table_names():
        op.drop_table("login_rate_limits")
