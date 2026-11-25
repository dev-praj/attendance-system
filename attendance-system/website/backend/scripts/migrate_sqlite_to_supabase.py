"""Copy a legacy SQLite database to the configured PostgreSQL database."""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, MetaData, Table, Uuid, create_engine, func, inspect, select
from sqlalchemy.dialects.postgresql import insert

BACKEND_DIR = Path(__file__).resolve().parents[1]
SOURCE = BACKEND_DIR / "data" / "attendance.db"
sys.path.insert(0, str(BACKEND_DIR))

UUID_NAMESPACE = uuid.UUID("da5939c3-7356-4d15-a050-e164cbd3f4fd")
BIOMETRIC_TABLES = {"face_embeddings", "enrollment_sessions"}


def migrated_uuid(table_name: str, value: object) -> str:
    """Keep existing UUIDs and deterministically convert legacy integer IDs."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(UUID_NAMESPACE, f"{table_name}:{value}"))


def migrated_row(table, row):
    values = {}
    audit_targets = {
        "teacher": "teachers",
        "student": "students",
        "class": "classes",
        "session": "class_sessions",
        "class_session": "class_sessions",
        "attendance_record": "attendance_records",
        "device": "devices",
    }
    for column in table.columns:
        name = column.name
        if name not in row:
            continue
        value = row[name]
        if (
            isinstance(value, datetime)
            and isinstance(column.type, DateTime)
            and column.type.timezone
            and value.tzinfo is None
        ):
            value = value.replace(tzinfo=timezone.utc)
        if table.name == "audit_log" and name == "entity_id" and value is not None:
            referenced_table = audit_targets.get(row.get("entity_type"))
            values[name] = (
                migrated_uuid(referenced_table, value)
                if referenced_table is not None
                else value
            )
            continue
        if value is None or not isinstance(column.type, Uuid):
            values[name] = value
            continue

        foreign_key = next(iter(column.foreign_keys), None)
        if foreign_key is not None:
            referenced_table = foreign_key.column.table.name
            value = migrated_uuid(referenced_table, value)
        elif name == "enrollment_session_id":
            value = migrated_uuid("enrollment_sessions", value)
        else:
            value = migrated_uuid(table.name, value)
        values[name] = value
    return values


def main():
    if not SOURCE.is_file():
        print(f"No local SQLite data found at {SOURCE}. Nothing to migrate.")
        return 0

    from app.database import engine as target_engine, verify_database_connection, verify_migrations
    from app.models import Base

    verify_database_connection(target_engine)
    verify_migrations(target_engine)
    source_engine = create_engine(f"sqlite:///{SOURCE.as_posix()}", pool_pre_ping=True)
    source_tables = set(inspect(source_engine).get_table_names())
    target_tables = set(inspect(target_engine).get_table_names())
    expected_tables = set(Base.metadata.tables) - BIOMETRIC_TABLES
    missing = expected_tables - target_tables
    if missing:
        raise RuntimeError(
            "Target schema is missing migrated tables. Run `python -m alembic upgrade head` first."
        )

    summaries = []
    biometric_skipped = []
    try:
        with source_engine.connect() as source, target_engine.begin() as target:
            for table in Base.metadata.sorted_tables:
                if table.name in BIOMETRIC_TABLES:
                    count = (
                        source.execute(
                            select(func.count()).select_from(
                                Table(table.name, MetaData(), autoload_with=source_engine)
                            )
                        ).scalar_one()
                        if table.name in source_tables
                        else 0
                    )
                    biometric_skipped.append((table.name, count))
                    continue
                if table.name not in source_tables:
                    summaries.append((table.name, 0, 0))
                    continue
                source_table = Table(table.name, MetaData(), autoload_with=source_engine)
                source_columns = set(source_table.c.keys())
                transferable_columns = source_columns & set(table.c.keys())
                rows = source.execute(select(*[source_table.c[name] for name in transferable_columns])).mappings()
                inserted = 0
                skipped = 0
                while True:
                    batch = rows.fetchmany(500)
                    if not batch:
                        break
                    converted_rows = [
                        migrated_row(table, row)
                        for row in batch
                    ]
                    result = target.execute(
                        insert(table).values(converted_rows).on_conflict_do_nothing()
                    )
                    batch_inserted = max(result.rowcount or 0, 0)
                    inserted += batch_inserted
                    skipped += len(batch) - batch_inserted
                summaries.append((table.name, inserted, skipped))

    finally:
        source_engine.dispose()

    for table_name, inserted, skipped in summaries:
        print(f"{table_name}: inserted={inserted}, already_present={skipped}")
    for table_name, count in biometric_skipped:
        print(f"{table_name}: skipped_biometric_rows={count} (not copied to Supabase)")
    print("SQLite source was not modified or deleted.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            f"Migration stopped ({type(error).__name__}). Check DATABASE_URL, migrations, and connectivity; "
            "no source SQLite data was deleted.",
            file=sys.stderr,
        )
        raise SystemExit(1)
