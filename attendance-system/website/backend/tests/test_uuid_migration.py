import uuid

from app import models
from scripts.migrate_sqlite_to_supabase import migrated_row, migrated_uuid


def test_legacy_ids_convert_deterministically_to_uuids():
    first = migrated_uuid("teachers", 17)
    assert first == migrated_uuid("teachers", 17)
    assert first != migrated_uuid("students", 17)
    assert str(uuid.UUID(first)) == first


def test_migration_preserves_foreign_key_relationships():
    class_row = migrated_row(
        models.SchoolClass.__table__,
        {"id": 29, "teacher_id": 17, "name": "Class", "code": "C-1",
         "schedule_days": "", "academic_term": "", "active": True, "is_demo": False},
    )
    assert class_row["id"] == migrated_uuid("classes", 29)
    assert class_row["teacher_id"] == migrated_uuid("teachers", 17)


def test_migration_keeps_existing_uuid_values():
    existing = str(uuid.uuid4())
    assert migrated_uuid("teachers", existing) == existing
