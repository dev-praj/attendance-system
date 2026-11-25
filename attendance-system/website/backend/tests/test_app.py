"""Authentication, tenant-scope, attendance, device, and enrollment tests."""

from datetime import date, datetime, time, timedelta, timezone
import uuid

import pytest
import numpy as np
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import api as api_module
from app import models, security
from app.database import Base, get_db
from app.face.coverage import classify_pose, is_complete, new_state, observe
from app.main import create_app
from app.repository import attendance_percent, calculate_session_status
from app.settings import settings


@pytest.fixture
def client(monkeypatch, tmp_path):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    test_factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = test_factory()
        try:
            yield db
        finally:
            db.close()

    application = create_app(test_engine)
    application.dependency_overrides[get_db] = override_db
    security._serializer = None
    settings.secret_key = "test-only-random-secret-with-adequate-length"
    settings.encryption_key = Fernet.generate_key().decode("ascii")
    settings.device_api_key_pepper = "test-device-pepper"
    api_module.face_store.path = tmp_path / "face-vault.enc"
    api_module.face_store.lock_path = tmp_path / "face-vault.enc.lock"
    monkeypatch.setattr(settings, "supabase_url", "")
    monkeypatch.setattr(settings, "supabase_anon_key", "")
    with TestClient(application) as test_client:
        test_client._test_factory = test_factory
        yield test_client
    test_engine.dispose()


def create_teacher(client, *, name="Admin", email="admin@example.com", role="admin", password="StrongPass!927"):
    with client._test_factory() as db:
        teacher = models.Teacher(name=name, email=email, password_hash=security.hash_password(password), role=role)
        db.add(teacher)
        db.commit()
        return teacher.id


def login(client, email="admin@example.com", password="StrongPass!927"):
    response = client.get("/api/auth/login")
    csrf = response.json()["csrf_token"]
    result = client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": csrf},
    )
    return result, csrf


def test_login_success_failure_lockout_and_audit(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "admin"
    with client._test_factory() as db:
        stored_session = db.query(models.LoginSession).one()
        assert stored_session.revoked_at is None

    wrong, csrf = login(client, password="WrongPassword!2")
    missing, _ = login(client, email="missing@example.com", password="WrongPassword!2")
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()
    for _ in range(4):
        response, csrf = login(client, password="WrongPassword!2")
        assert response.status_code == 401
    with client._test_factory() as db:
        admin = db.query(models.Teacher).filter_by(email="admin@example.com").one()
        assert admin.locked_until is not None
        assert db.query(models.AuditLog).filter_by(action="failed_login").count() == 5


def test_login_session_is_database_backed_and_revoked_on_logout(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    assert client.get("/api/auth/me").status_code == 200
    result = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert result.status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    with client._test_factory() as db:
        stored_session = db.query(models.LoginSession).one()
        assert stored_session.revoked_at is not None


def test_supabase_login_requires_an_approved_teacher_profile(client, monkeypatch):
    create_teacher(client)
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "test-publishable-key")

    class AuthResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"user": {"email": "admin@example.com"}}

    def authenticate(url, **kwargs):
        assert url == "https://example.supabase.co/auth/v1/token"
        assert kwargs["params"] == {"grant_type": "password"}
        assert kwargs["headers"]["apikey"] == "test-publishable-key"
        assert kwargs["json"] == {"email": "admin@example.com", "password": "supabase-password"}
        return AuthResponse()

    monkeypatch.setattr("app.api.httpx.post", authenticate)
    response, _ = login(client, password="supabase-password")
    assert response.status_code == 200


def test_supabase_auth_does_not_grant_access_without_teacher_profile(client, monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "test-publishable-key")

    class AuthResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"user": {"email": "new@example.com"}}

    monkeypatch.setattr("app.api.httpx.post", lambda *args, **kwargs: AuthResponse())
    response, _ = login(client, email="new@example.com", password="supabase-password")
    assert response.status_code == 403
    assert "not approved" in response.json()["detail"]


def test_supabase_login_ignores_local_password_when_provider_rejects(client, monkeypatch):
    create_teacher(client, password="local-password-that-is-valid")
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "test-publishable-key")

    class AuthResponse:
        status_code = 400

    monkeypatch.setattr("app.api.httpx.post", lambda *args, **kwargs: AuthResponse())
    response, _ = login(client, password="correct-horse-battery")
    assert response.status_code == 401


def test_supabase_login_reports_provider_outage(client, monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "test-publishable-key")

    def unavailable(*args, **kwargs):
        import httpx

        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr("app.api.httpx.post", unavailable)
    response, _ = login(client, email="new@example.com", password="supabase-password")
    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]


def test_supabase_login_rejects_partial_configuration(client, monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "")
    response, _ = login(client, email="new@example.com", password="supabase-password")
    assert response.status_code == 503
    assert "SUPABASE_ANON_KEY" in response.json()["detail"]


def test_every_teacher_api_family_requires_authentication(client):
    paths = ("/api/dashboard", "/api/classes", "/api/students", "/api/settings", "/api/faces", "/api/reports")
    for path in paths:
        assert client.get(path).status_code == 401
    assert client.get("/api/health").status_code == 200


def test_teacher_cannot_read_another_teachers_class_or_students(client):
    owner_id = create_teacher(client, name="Teacher One", email="one@example.com", role="teacher")
    other_id = create_teacher(client, name="Teacher Two", email="two@example.com", role="teacher")
    with client._test_factory() as db:
        item = models.SchoolClass(name="Private", code="PRIVATE", teacher_id=other_id)
        db.add(item)
        db.flush()
        student = models.Student(name="Private student", roll_no="R-2")
        db.add(student)
        db.flush()
        db.add(models.ClassEnrollment(class_id=item.id, student_id=student.id))
        db.commit()
        class_id, student_id = item.id, student.id
    response, _ = login(client, "one@example.com")
    assert response.status_code == 200
    assert client.get(f"/api/classes/{class_id}/detail").status_code == 404
    assert client.get(f"/api/students/{student_id}").status_code == 404
    assert owner_id != other_id


def test_teacher_cannot_claim_another_teachers_student_for_face_enrollment(client):
    own_id = create_teacher(client, name="Teacher One", email="one@example.com", role="teacher")
    other_id = create_teacher(client, name="Teacher Two", email="two@example.com", role="teacher")
    with client._test_factory() as db:
        own_class = models.SchoolClass(name="Own", code="OWN", teacher_id=own_id)
        other_class = models.SchoolClass(name="Other", code="OTHER", teacher_id=other_id)
        student = models.Student(name="Private student", roll_no="R-2")
        db.add_all([own_class, other_class, student])
        db.flush()
        db.add(models.ClassEnrollment(class_id=other_class.id, student_id=student.id))
        db.commit()
        own_class_id = own_class.id
    response, _ = login(client, "one@example.com")
    assert response.status_code == 200
    payload = {
        "name": "Changed name",
        "roll_no": "R-2",
        "consent": True,
    }
    result = client.post(
        "/api/face/enrollments",
        json=payload,
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert result.status_code == 404


def test_face_enrollment_without_subjects_creates_one_unassigned_student(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    result = client.post(
        "/api/face/enrollments",
        json={"name": "Face Test Student", "roll_no": " san-100 ", "consent": True},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert result.status_code == 200
    with client._test_factory() as db:
        student = db.query(models.Student).one()
        assert student.roll_no == "SAN-100"
        assert db.query(models.ClassEnrollment).count() == 0
        assert student.id == result.json()["student_id"]
    assert api_module.face_store.get_session(result.json()["session_id"]) is not None


def test_face_reenrollment_normalizes_san_and_reuses_student(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    headers = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    first = client.post(
        "/api/face/enrollments",
        json={"name": "Face Test Student", "roll_no": "san-200", "consent": True},
        headers=headers,
    )
    second = client.post(
        "/api/face/enrollments",
        json={"name": "face test student", "roll_no": " SAN-200 ", "consent": True},
        headers=headers,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["student_id"] == second.json()["student_id"]
    with client._test_factory() as db:
        assert db.query(models.Student).count() == 1
        assert db.query(models.Student).one().roll_no == "SAN-200"


def test_teacher_can_reenroll_own_unassigned_student_but_not_another_teachers(client):
    owner_id = create_teacher(client, name="Teacher One", email="one@example.com", role="teacher")
    create_teacher(client, name="Teacher Two", email="two@example.com", role="teacher")
    response, _ = login(client, "one@example.com")
    assert response.status_code == 200
    headers = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    payload = {"name": "Face Test Student", "roll_no": "SAN-250", "consent": True}
    first = client.post("/api/face/enrollments", json=payload, headers=headers)
    repeat = client.post("/api/face/enrollments", json=payload, headers=headers)
    assert first.status_code == repeat.status_code == 200
    with client._test_factory() as db:
        student = db.query(models.Student).one()
        assert student.consent_recorded_by == owner_id
        assert db.query(models.ClassEnrollment).count() == 0

    client.post("/api/auth/logout", headers={"X-CSRF-Token": client.cookies.get("csrf_token")})
    response, _ = login(client, "two@example.com")
    assert response.status_code == 200
    denied = client.post(
        "/api/face/enrollments",
        json=payload,
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert denied.status_code == 404


def test_face_reenrollment_rejects_different_name_for_same_san(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    headers = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    first = client.post(
        "/api/face/enrollments",
        json={"name": "Original Name", "roll_no": "SAN-300", "consent": True},
        headers=headers,
    )
    conflict = client.post(
        "/api/face/enrollments",
        json={"name": "Different Name", "roll_no": "san-300", "consent": True},
        headers=headers,
    )
    assert first.status_code == 200
    assert conflict.status_code == 409
    assert "different student name" in conflict.json()["detail"]
    with client._test_factory() as db:
        assert db.query(models.Student).count() == 1
        assert db.query(models.Student).one().name == "Original Name"


def test_student_creation_rejects_existing_san_after_normalization(client):
    create_teacher(client)
    with client._test_factory() as db:
        db.add(models.Student(name="Legacy Student", roll_no="san-350"))
        db.commit()
    response, _ = login(client)
    assert response.status_code == 200
    result = client.post(
        "/api/students",
        json={"name": "Different Student", "roll_no": " SAN-350 "},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert result.status_code == 409
    with client._test_factory() as db:
        assert db.query(models.Student).count() == 1


def test_admin_subject_assignment_is_idempotent_and_unassign_is_idempotent(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    with client._test_factory() as db:
        student = models.Student(name="Subject Test Student", roll_no="SAN-400")
        item = models.SchoolClass(name="Subject Test Class", code="SUBJ-TEST", teacher_id=db.query(models.Teacher).one().id)
        db.add_all([student, item])
        db.commit()
        san, class_id = student.roll_no, item.id
    headers = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    body = {"san": san, "class_ids": [class_id, class_id]}
    first = client.post("/api/students/subjects", json=body, headers=headers)
    second = client.post("/api/students/subjects", json={"san": san, "class_ids": [class_id]}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert [item["id"] for item in second.json()["subjects"]] == [class_id]
    with client._test_factory() as db:
        assert db.query(models.ClassEnrollment).count() == 1
        assert db.query(models.AuditLog).filter_by(action="student_subjects_assigned").count() == 1

    params = {"san": san, "class_id": class_id}
    removed = client.delete("/api/students/subjects", params=params, headers=headers)
    removed_again = client.delete("/api/students/subjects", params=params, headers=headers)
    assert removed.status_code == removed_again.status_code == 200
    assert removed_again.json()["subjects"] == []
    with client._test_factory() as db:
        assert db.query(models.ClassEnrollment).count() == 0
        assert db.query(models.AuditLog).filter_by(action="student_subject_unassigned").count() == 1


def test_subject_lookup_and_assignment_return_404_for_unknown_san(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    assert client.get("/api/students/lookup", params={"san": "MISSING-1"}).status_code == 404
    headers = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    assigned = client.post(
        "/api/students/subjects",
        json={"san": "MISSING-1", "class_ids": ["00000000-0000-0000-0000-000000000000"]},
        headers=headers,
    )
    assert assigned.status_code == 404


def test_subject_assignment_routes_are_admin_only(client):
    teacher_id = create_teacher(client, name="Teacher", email="teacher@example.com", role="teacher")
    with client._test_factory() as db:
        student = models.Student(name="Subject Test Student", roll_no="SAN-500")
        item = models.SchoolClass(name="Subject Test Class", code="SUBJ-TEACHER", teacher_id=teacher_id)
        db.add_all([student, item])
        db.commit()
        class_id = item.id
    response, _ = login(client, "teacher@example.com")
    assert response.status_code == 200
    assert client.get("/api/students/lookup", params={"san": "SAN-500"}).status_code == 403
    headers = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    assert client.post(
        "/api/students/subjects", json={"san": "SAN-500", "class_ids": [class_id]}, headers=headers
    ).status_code == 403
    assert client.delete(
        "/api/students/subjects", params={"san": "SAN-500", "class_id": class_id}, headers=headers
    ).status_code == 403


def test_legacy_class_roster_assignment_paths_are_admin_only(client):
    teacher_id = create_teacher(client, name="Teacher", email="teacher@example.com", role="teacher")
    with client._test_factory() as db:
        student = models.Student(name="Subject Test Student", roll_no="SAN-550")
        item = models.SchoolClass(name="Subject Test Class", code="SUBJ-LEGACY", teacher_id=teacher_id)
        db.add_all([student, item])
        db.commit()
        student_id, class_id = student.id, item.id
    response, _ = login(client, "teacher@example.com")
    assert response.status_code == 200
    headers = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    assert client.post(
        f"/api/classes/{class_id}/students/{student_id}", headers=headers
    ).status_code == 403
    assert client.post(
        f"/api/classes/{class_id}/students/bulk", json={"roll_numbers": ["SAN-550"]}, headers=headers
    ).status_code == 403
    assert client.delete(
        f"/api/classes/{class_id}/students/{student_id}", headers=headers
    ).status_code == 403
    assert client.post(
        "/api/students",
        json={"name": "Another Student", "roll_no": "SAN-551", "class_ids": [class_id]},
        headers=headers,
    ).status_code == 403


def test_enrollment_rejects_legacy_subject_assignment_field(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    result = client.post(
        "/api/face/enrollments",
        json={"name": "Face Test Student", "roll_no": "SAN-600", "consent": True, "class_ids": []},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert result.status_code == 422


def test_csrf_is_required_for_state_changes(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    body = {"name": "Math", "code": "MATH-1", "schedule_days": "", "academic_term": ""}
    assert client.post("/api/classes", json=body).status_code == 403
    csrf = client.cookies.get("csrf_token")
    created = client.post("/api/classes", json=body, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 200


def test_attendance_percentage_and_status_edge_cases():
    records = [
        models.AttendanceRecord(status="present"),
        models.AttendanceRecord(status="late"),
        models.AttendanceRecord(status="absent"),
        models.AttendanceRecord(status="excused"),
    ]
    assert attendance_percent([], True) is None
    assert attendance_percent(records, True) == pytest.approx(66.67)
    assert attendance_percent(records, False) == pytest.approx(33.33)
    start = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    assert calculate_session_status(start + timedelta(minutes=10), date(2026, 1, 1), time(9), time(10), 10) == "present"
    assert calculate_session_status(start + timedelta(minutes=11), date(2026, 1, 1), time(9), time(10), 10) == "late"
    assert calculate_session_status(start + timedelta(hours=1), date(2026, 1, 1), time(9), time(10), 10) == "absent"
    assert calculate_session_status(None, date(2026, 1, 1), time(9), time(10), 10) == "absent"


def test_device_event_deduplicates_same_direction_within_thirty_seconds(client):
    create_teacher(client)
    device_key = "device-key-once"
    digest = security.device_key_hash(device_key)
    with client._test_factory() as db:
        db.add(models.Device(name="Door", api_key_hash=digest))
        db.add(models.Student(name="Student", roll_no="R-1"))
        db.commit()
    timestamp = datetime.now(timezone.utc)
    payload = {"student_roll_no": "R-1", "direction": "in", "timestamp": timestamp.isoformat(), "score": 0.8}
    first = client.post("/api/device/events", json=payload, headers={"X-Device-Key": device_key})
    second_payload = {**payload, "timestamp": (timestamp + timedelta(seconds=12)).isoformat()}
    second = client.post("/api/device/events", json=second_payload, headers={"X-Device-Key": device_key})
    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert second.status_code == 200 and second.json()["duplicate"] is True
    with client._test_factory() as db:
        assert db.query(models.AttendanceEvent).count() == 1


def test_teacher_face_list_never_returns_embeddings(client):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    response = client.get("/api/faces")
    assert response.status_code == 200
    assert "vector" not in response.text
    assert "encrypted_vector" not in response.text


def test_face_templates_persist_only_in_encrypted_vault_file(client):
    student_id = _add_completed_face(client)
    vault_path = api_module.face_store.path
    vault_bytes = vault_path.read_bytes()
    vector_bytes = np.eye(1, 128, dtype=np.float32).reshape(-1).tobytes()
    assert vector_bytes not in vault_bytes

    reloaded_store = type(api_module.face_store)(vault_path)
    assert len(reloaded_store.list_templates(student_id)) == 3
    reloaded_store.delete_templates(student_id)
    assert reloaded_store.list_templates(student_id) == []


def _add_completed_face(client):
    vector = np.zeros(128, dtype=np.float32)
    vector[0] = 1.0
    with client._test_factory() as db:
        student = models.Student(
            name="Face Test Student",
            roll_no="FACE-1",
            consent_given_at=datetime.now(timezone.utc),
        )
        db.add(student)
        db.flush()
        records = []
        for _ in range(3):
            records.append(api_module.FaceVectorRecord(
                id=str(uuid.uuid4()),
                student_id=student.id,
                enrollment_session_id=None,
                pose="CENTER",
                encrypted_vector=Fernet(settings.encryption_key.encode("ascii")).encrypt(vector.tobytes()),
                model_version="sface_2021dec",
                quality=1.0,
                yaw=0.0,
                pitch=0.0,
                created_at=datetime.now(timezone.utc),
            ))
        db.commit()
        api_module.face_store.replace_templates(student.id, records)
        assert db.query(models.FaceEmbedding).filter_by(student_id=student.id).count() == 0
        return student.id


def _stub_face_engine(monkeypatch, vector, face_count=1):
    monkeypatch.setattr(api_module.engine, "detector", object())
    monkeypatch.setattr(api_module.engine, "recognizer", object())
    monkeypatch.setattr(api_module, "_decode_camera_frame", lambda _: np.zeros((80, 80, 3), dtype=np.uint8))
    monkeypatch.setattr(api_module.engine, "detect", lambda _: [np.zeros(15, dtype=np.float32)] * face_count)
    monkeypatch.setattr(api_module.engine, "quality", lambda *_: (True, None, 100.0))
    monkeypatch.setattr(api_module.engine, "align_and_embed", lambda *_: vector)


def test_face_verification_records_attendance_only_after_acceptance(client, monkeypatch):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    _add_completed_face(client)
    probe = np.zeros(128, dtype=np.float32)
    probe[0] = 1.0
    _stub_face_engine(monkeypatch, probe)

    response = client.post(
        "/api/face/verify",
        json={"frames": ["A" * 32] * 8, "direction": "in"},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["accepted"] is True
    assert result["good_frames"] == 8
    assert result["votes"][0]["votes"] == 8
    assert len(result["frame_results"]) == 8
    assert "vector" not in response.text
    assert "encrypted_vector" not in response.text
    with client._test_factory() as db:
        event = db.query(models.AttendanceEvent).one()
        assert event.direction == "in"
        assert event.source == "teacher_scan"


def test_face_verification_rejects_below_threshold_without_attendance(client, monkeypatch):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    _add_completed_face(client)
    impostor = np.zeros(128, dtype=np.float32)
    impostor[1] = 1.0
    _stub_face_engine(monkeypatch, impostor)

    response = client.post(
        "/api/face/verify",
        json={"frames": ["A" * 32] * 8},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["accepted"] is False
    assert all(frame["vote"] is None for frame in result["frame_results"])
    with client._test_factory() as db:
        assert db.query(models.AttendanceEvent).count() == 0


def test_face_verification_rejects_unclear_frames(client, monkeypatch):
    create_teacher(client)
    response, _ = login(client)
    assert response.status_code == 200
    _add_completed_face(client)
    probe = np.zeros(128, dtype=np.float32)
    probe[0] = 1.0
    _stub_face_engine(monkeypatch, probe)
    calls = iter(range(8))
    monkeypatch.setattr(
        api_module.engine,
        "detect",
        lambda _: [] if next(calls) == 3 else [np.zeros(15, dtype=np.float32)],
    )

    response = client.post(
        "/api/face/verify",
        json={"frames": ["A" * 32] * 8},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert response.status_code == 200
    assert response.json()["clear_scan"] is False
    assert response.json()["good_frames"] == 7
    with client._test_factory() as db:
        assert db.query(models.AttendanceEvent).count() == 0


def test_pose_coverage_requires_all_nine_measured_poses():
    state = new_state()
    for _ in range(40 * 30):
        pose = classify_pose(0, 0)
        observe(state, pose, True)
    assert not is_complete(state)
    assert state["counts"]["CENTER"] == 3
    assert all(state["counts"][pose] == 0 for pose in (
        "LEFT", "RIGHT", "UP", "DOWN", "UP-LEFT", "UP-RIGHT", "DOWN-LEFT", "DOWN-RIGHT"
    ))
    poses = {
        "CENTER": (0, 0), "LEFT": (-22, 0), "RIGHT": (22, 0), "UP": (0, -12),
        "DOWN": (0, 12), "UP-LEFT": (-14, -9), "UP-RIGHT": (14, -9),
        "DOWN-LEFT": (-14, 9), "DOWN-RIGHT": (14, 9),
    }
    for yaw, pitch in poses.values():
        for _ in range(9):
            observe(state, classify_pose(yaw, pitch), True)
    assert is_complete(state)


def test_pose_coverage_captures_stable_frames_at_spaced_intervals():
    state = new_state()
    pose = classify_pose(0, 0)
    assert observe(state, pose, True, 0.0) is False
    assert observe(state, pose, True, 0.1) is False
    assert observe(state, pose, True, 0.2) is True
    assert observe(state, pose, True, 0.25) is False
    assert observe(state, pose, True, 0.4) is True
    assert observe(state, pose, True, 0.6) is True
    assert state["counts"]["CENTER"] == 3
