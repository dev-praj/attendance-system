import pytest
from sqlalchemy.engine import make_url

from app.settings import _database_url_from_env


def test_database_url_rejects_supabase_project_https_url(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "https://project.supabase.co")
    with pytest.raises(RuntimeError, match="not the Supabase project HTTPS URL"):
        _database_url_from_env()


def test_database_url_requires_postgres_outside_tests(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")
    with pytest.raises(RuntimeError, match="must be a PostgreSQL URI"):
        _database_url_from_env()


def test_database_url_forces_ssl_and_connection_timeout(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://test-user:test-password@pooler.example:5432/postgres?sslmode=disable",
    )
    parsed = make_url(_database_url_from_env())
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.query["sslmode"] == "require"
    assert parsed.query["connect_timeout"] == "10"


def test_sqlite_url_is_only_allowed_in_test_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert _database_url_from_env() == "sqlite:///:memory:"
