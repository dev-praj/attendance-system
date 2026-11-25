"""PostgreSQL engine setup. SQLite is only enabled by the isolated test environment."""

import logging
import time

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .settings import settings


logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


is_sqlite_test = settings.environment == "test" and settings.database_url.startswith("sqlite:")
connect_args = {"check_same_thread": False} if is_sqlite_test else {}
if not is_sqlite_test:
    if make_url(settings.database_url).port == 6543:
        connect_args["prepare_threshold"] = None

engine_options = {"pool_pre_ping": True}
if not is_sqlite_test:
    engine_options.update(pool_size=5, max_overflow=5, pool_recycle=300)
engine = create_engine(settings.database_url, connect_args=connect_args, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def verify_database_connection(database_engine=engine, *, attempts=3) -> None:
    for attempt in range(attempts):
        try:
            with database_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except SQLAlchemyError as error:
            if attempt + 1 == attempts:
                logger.error("Database connection check failed after %s attempts.", attempts)
                raise RuntimeError(
                    "Cannot connect to Supabase PostgreSQL. Check DATABASE_URL, SSL, network access, "
                    "and whether the Supabase project is paused."
                ) from error
            time.sleep(1 + attempt)


def verify_migrations(database_engine=engine, *, expected_head="0006_move_biometrics_out_of_database") -> None:
    inspector = inspect(database_engine)
    if "alembic_version" not in inspector.get_table_names():
        raise RuntimeError("Database migrations are missing. Run `python -m alembic upgrade head` from backend.")
    with database_engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if revision != expected_head:
        raise RuntimeError(
            f"Database schema is not at the expected Alembic revision ({expected_head}). "
            "Run `python -m alembic upgrade head` from backend."
        )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
