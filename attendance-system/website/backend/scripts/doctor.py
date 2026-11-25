"""Check deployment configuration and Supabase readiness without printing secrets."""

import os
import sys
import uuid
from pathlib import Path

import cv2
from dotenv import dotenv_values, load_dotenv
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
WEBSITE_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))


results = []


def report(name, passed, reason, hint):
    results.append(bool(passed))
    print(f"{'PASS' if passed else 'FAIL'} {name}: {reason}")
    if not passed:
        print(f"  Fix: {hint}")


def main():
    env_values = dotenv_values(WEBSITE_DIR / ".env")
    required = (
        "DATABASE_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SECRET_KEY",
        "ENCRYPTION_KEY", "DEVICE_API_KEY_PEPPER",
    )
    missing = [key for key in required if not (os.getenv(key) or env_values.get(key) or "").strip()]
    report("environment", not missing,
           "all required values are present" if not missing else f"missing {', '.join(missing)}",
           "Set the listed variables in website/.env; DATABASE_URL must come from Supabase Connect.")

    raw_url = (os.getenv("DATABASE_URL") or env_values.get("DATABASE_URL") or "").strip()
    parsed_url = None
    try:
        parsed_url = make_url(raw_url) if raw_url else None
        url_valid = bool(parsed_url and parsed_url.drivername in {"postgres", "postgresql", "postgresql+psycopg"})
    except (TypeError, ValueError):
        url_valid = False
    report("database-url", url_valid,
           "PostgreSQL connection URI is valid" if url_valid else "DATABASE_URL is not a PostgreSQL connection URI",
           "Copy the PostgreSQL URI from Supabase Connect (prefer Session pooler, port 5432); do not use the project HTTPS URL.")

    secrets_valid = (
        len((os.getenv("SECRET_KEY") or env_values.get("SECRET_KEY") or "")) >= 32
        and len((os.getenv("DEVICE_API_KEY_PEPPER") or env_values.get("DEVICE_API_KEY_PEPPER") or "")) >= 32
    )
    try:
        from cryptography.fernet import Fernet

        Fernet((os.getenv("ENCRYPTION_KEY") or env_values.get("ENCRYPTION_KEY") or "").encode("ascii"))
        encryption_valid = True
    except (ValueError, UnicodeEncodeError):
        encryption_valid = False
    report("secret-formats", secrets_valid and encryption_valid,
           "session, device and Fernet key formats are valid" if secrets_valid and encryption_valid else "one or more key formats are invalid",
           "Run python scripts/generate_secrets.py and set its output in website/.env; back up ENCRYPTION_KEY.")

    if not url_valid or missing:
        for name in (
            "database-ssl", "alembic-head", "tables", "row-level-security", "active-admin",
            "encryption-round-trip", "model-load", "database-round-trip",
        ):
            report(name, False, "not checked because required connection configuration is invalid or incomplete",
                   "Correct the environment configuration and rerun python scripts/doctor.py.")
        print(f"Summary: {sum(results)} PASS, {len(results) - sum(results)} FAIL")
        return 1

    load_dotenv(WEBSITE_DIR / ".env", override=False)
    try:
        from sqlalchemy import inspect, text
        from app.database import SessionLocal, engine, verify_database_connection, verify_migrations
        from app.main import _verify_encryption_key
        from app import models
        from app.face.engine import engine as face_engine

        verify_database_connection(engine)
        with engine.connect() as connection:
            is_ssl = connection.execute(text(
                "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
            )).scalar_one_or_none()
        report("database-ssl", bool(is_ssl), "connection is using SSL" if is_ssl else "connection is not using SSL",
               "Use a Supabase PostgreSQL connection and allow sslmode=require.")

        try:
            verify_migrations(engine)
            migration_ok = True
        except RuntimeError:
            migration_ok = False
        report("alembic-head", migration_ok, "database is at the expected Alembic revision" if migration_ok else "database is not at migration head",
               "From website/backend run python -m alembic upgrade head.")

        inspector = inspect(engine)
        # These legacy ORM mappings are retained only so pre-0006 migrations can
        # export old records; current biometric data lives in the encrypted vault.
        required_tables = (
            set(models.Base.metadata.tables)
            - {"face_embeddings", "enrollment_sessions"}
        ) | {"alembic_version"}
        missing_tables = required_tables - set(inspector.get_table_names(schema="public"))
        report("tables", not missing_tables,
               "all application tables exist" if not missing_tables else f"missing tables: {', '.join(sorted(missing_tables))}",
               "Apply Alembic migrations with python -m alembic upgrade head.")

        with engine.connect() as connection:
            rls_rows = connection.execute(text(
                "SELECT c.relname, c.relrowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
            )).all()
        rls_off = [name for name, enabled in rls_rows if not enabled]
        report("row-level-security", bool(rls_rows) and not rls_off,
               "RLS is enabled on all public tables" if rls_rows and not rls_off else
               f"RLS is off or no public tables were found: {', '.join(rls_off)}",
               "Apply the RLS migration; do not add anon/authenticated policies.")

        with SessionLocal() as db:
            admin_exists = db.query(models.Teacher.id).filter_by(role="admin", active=True).first() is not None
            report("active-admin", admin_exists, "an active administrator exists" if admin_exists else "no active administrator exists",
                   "Run python scripts/create_admin.py after applying migrations.")
            try:
                _verify_encryption_key(db)
                encryption_ok = True
            except RuntimeError:
                encryption_ok = False
            report("encryption-round-trip", encryption_ok,
                   "key check and encrypted face vault templates can be decrypted" if encryption_ok else "key check or encrypted face vault validation failed",
                   "Restore the original ENCRYPTION_KEY from secure backup; do not rotate it while encrypted face data exists.")

            try:
                probe_key = f"doctor_probe_{uuid.uuid4().hex}"
                with SessionLocal() as probe_db:
                    with probe_db.begin():
                        probe_db.add(models.AppSetting(key=probe_key, value="doctor"))
                        probe_db.flush()
                        found = probe_db.get(models.AppSetting, probe_key)
                        probe_db.delete(found)
                        probe_db.flush()
                rowtrip_ok = True
            except Exception:
                db.rollback()
                rowtrip_ok = False
            report("database-round-trip", rowtrip_ok,
                   "temporary insert/read/delete succeeded" if rowtrip_ok else "temporary round-trip failed",
                   "Check database grants, connectivity, and migration state.")

        try:
            face_engine.load()
            model_ok = face_engine.detector is not None and face_engine.recognizer is not None
        except (FileNotFoundError, RuntimeError, cv2.error):
            model_ok = False
        report("model-load", model_ok, "YuNet and SFace models loaded" if model_ok else "models are missing or could not be loaded",
               "From website/backend run python scripts/download_models.py.")
        return 0 if all(results) else 1
    except Exception as error:
        # Do not print exception strings because driver messages can contain connection details.
        report("database-checks", False, f"checks could not complete ({type(error).__name__})",
               "Verify the Supabase URI, network, SSL and project status; rerun the doctor.")
        return 1
    finally:
        print(f"Summary: {sum(results)} PASS, {len(results) - sum(results)} FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
