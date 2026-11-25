"""Environment-backed settings; secrets are never committed to the project."""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url


BACKEND_DIR = Path(__file__).resolve().parents[1]
WEBSITE_DIR = BACKEND_DIR.parent
load_dotenv(WEBSITE_DIR / ".env")


def _database_url_from_env() -> str:
    raw = os.getenv("DATABASE_URL", "").strip()
    environment = os.getenv("APP_ENV", "development").strip().lower()
    if environment == "test" and raw.startswith("sqlite://"):
        return raw
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is required. Set it to the Supabase PostgreSQL connection URI "
            "(prefer the Session pooler on port 5432)."
        )
    try:
        url = make_url(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "DATABASE_URL is invalid. Use the PostgreSQL connection URI from Supabase Connect."
        ) from error
    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise RuntimeError(
            "DATABASE_URL must be a PostgreSQL URI from Supabase Connect, not the Supabase project HTTPS URL."
        )
    url = url.set(drivername="postgresql+psycopg")
    query = dict(url.query)
    query["sslmode"] = "require"
    query["connect_timeout"] = "10"
    return url.set(query=query).render_as_string(hide_password=False)


class Settings:
    environment = os.getenv("APP_ENV", "development").strip().lower()
    database_url = _database_url_from_env()
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    secret_key = os.getenv("SECRET_KEY", "")
    encryption_key = os.getenv("ENCRYPTION_KEY", "")
    device_api_key_pepper = os.getenv("DEVICE_API_KEY_PEPPER", "")
    face_store_path = Path(
        os.getenv("FACE_STORE_PATH", str(BACKEND_DIR / "data" / "face-vault.enc"))
    ).expanduser()
    app_origin = os.getenv("APP_ORIGIN", "http://localhost:5173").rstrip("/")
    secure_cookies = environment == "production"

    @classmethod
    def session_secret(cls):
        return cls.secret_key

    @classmethod
    def validate_runtime(cls):
        if cls.environment == "test":
            return
        missing = [
            name for name, value in (
                ("SECRET_KEY", cls.secret_key),
                ("ENCRYPTION_KEY", cls.encryption_key),
                ("DEVICE_API_KEY_PEPPER", cls.device_api_key_pepper),
                ("SUPABASE_URL", cls.supabase_url),
                ("SUPABASE_ANON_KEY", cls.supabase_anon_key),
            ) if not value
        ]
        if cls.environment == "production" and not os.getenv("FACE_STORE_PATH", "").strip():
            missing.append("FACE_STORE_PATH (persistent encrypted face-store volume)")
        if missing:
            raise RuntimeError(f"Required environment variables are missing: {', '.join(missing)}.")
        if len(cls.secret_key) < 32 or cls.secret_key.casefold() in {"changeme", "default", "secret"}:
            raise RuntimeError("SECRET_KEY must be a unique value of at least 32 characters.")
        if len(cls.device_api_key_pepper) < 32:
            raise RuntimeError("DEVICE_API_KEY_PEPPER must be a unique value of at least 32 characters.")
        try:
            from cryptography.fernet import Fernet

            Fernet(cls.encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise RuntimeError("ENCRYPTION_KEY must be a valid Fernet key; generate one with scripts/generate_secrets.py.") from error


settings = Settings()
