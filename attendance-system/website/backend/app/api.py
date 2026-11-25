"""Teacher-only API. Access control is enforced on every data route."""

import base64
import binascii
import csv
import io
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import httpx
import cv2
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, repository, schemas, security
from .database import get_db
from .face.coverage import classify_pose, is_complete, new_state, observe, verify_embedding_quality
from .face.engine import engine
from .face.matching import DEFAULT_THRESHOLD, decide_votes, frame_vote, score_embedding
from .face.records import FaceVectorRecord
from .face.store import face_store
from .face import timing as face_timing
from .settings import settings


router = APIRouter(prefix="/api")
PUBLIC_CACHE_HEADERS = {"Cache-Control": "no-store"}
logger = logging.getLogger(__name__)


def _fernet():
    if not settings.encryption_key:
        raise HTTPException(status_code=503, detail="Face encryption is not configured.")
    try:
        return Fernet(settings.encryption_key.encode("ascii"))
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=503, detail="ENCRYPTION_KEY is invalid.") from error


def _decode_vector(value: bytes) -> np.ndarray:
    try:
        raw = _fernet().decrypt(value)
    except InvalidToken as error:
        raise HTTPException(status_code=500, detail="Stored face data could not be decrypted.") from error
    vector = np.frombuffer(raw, dtype=np.float32)
    if vector.shape != (128,) or not np.isfinite(vector).all():
        raise HTTPException(status_code=500, detail="Stored face data is invalid.")
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise HTTPException(status_code=500, detail="Stored face data is invalid.")
    return vector / norm


def _retained_embedding_rows(stored, filtered):
    retained = []
    for pose, vectors in filtered.items():
        unmatched = [np.asarray(vector, dtype=np.float32) for vector in vectors]
        for row in stored:
            if row.pose != pose:
                continue
            vector = _decode_vector(row.encrypted_vector)
            match = next(
                (index for index, value in enumerate(unmatched) if np.array_equal(vector, value)),
                None,
            )
            if match is not None:
                retained.append(row)
                unmatched.pop(match)
    return retained


def _encrypt_vector(vector: np.ndarray) -> bytes:
    normalized = np.asarray(vector, dtype=np.float32).reshape(-1)
    if normalized.shape != (128,) or not np.isfinite(normalized).all():
        raise ValueError("Invalid SFace embedding.")
    return _fernet().encrypt(normalized.tobytes())


def _encode_transient_vector(vector: np.ndarray) -> str:
    normalized = np.asarray(vector, dtype=np.float32).reshape(-1)
    if normalized.shape != (128,) or not np.isfinite(normalized).all():
        raise ValueError("Invalid transient SFace embedding.")
    return _fernet().encrypt(normalized.tobytes()).decode("ascii")


def _decode_transient_vector(value: str) -> np.ndarray:
    try:
        vector = np.frombuffer(_fernet().decrypt(value.encode("ascii")), dtype=np.float32)
    except (InvalidToken, ValueError, UnicodeEncodeError) as error:
        raise HTTPException(status_code=500, detail="Stored enrollment verification state is invalid.") from error
    if vector.shape != (128,) or not np.isfinite(vector).all():
        raise HTTPException(status_code=500, detail="Stored enrollment verification state is invalid.")
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise HTTPException(status_code=500, detail="Stored enrollment verification state is invalid.")
    return vector / norm


def _audit(request: Request, db: Session, user: models.Teacher | None, action: str, **kwargs):
    security.audit(
        db,
        action,
        actor_id=user.id if user else None,
        ip_address=request.client.host if request.client else None,
        **kwargs,
    )


def _teacher_scope(user: models.Teacher) -> str | None:
    return None if user.role == "admin" else user.id


def _setting(db: Session, key: str, default: str) -> str:
    row = db.get(models.AppSetting, key)
    return row.value if row else default


def _ensure_face_engine():
    if engine.detector is not None and engine.recognizer is not None:
        return
    try:
        engine.load()
    except (FileNotFoundError, RuntimeError, cv2.error) as error:
        logger.exception("Face model initialization failed.")
        raise HTTPException(
            status_code=503,
            detail="Face recognition models are unavailable. Check the deployment model files and server logs.",
        ) from error


def _decode_camera_frame(encoded: str):
    try:
        raw = base64.b64decode(encoded, validate=True)
        frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    except (ValueError, binascii.Error, cv2.error) as error:
        raise HTTPException(status_code=422, detail="A camera frame is not a valid encoded image.") from error
    if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
        raise HTTPException(status_code=422, detail="A camera frame is not a valid color image.")
    if frame.shape[1] > 1280 or frame.shape[0] > 1280:
        raise HTTPException(status_code=422, detail="Camera frames must be no larger than 1280 pixels per side.")
    return frame


def _supabase_authenticate(email: str, password: str) -> bool | None:
    has_supabase_settings = bool(settings.supabase_url or settings.supabase_anon_key)
    if not has_supabase_settings:
        return None
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(
            status_code=503,
            detail="Supabase authentication is incomplete. Configure SUPABASE_URL and SUPABASE_ANON_KEY.",
        )

    try:
        response = httpx.post(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": settings.supabase_anon_key},
            json={"email": email, "password": password},
            timeout=8.0,
        )
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=503, detail="Supabase authentication timed out. Try again.") from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="Supabase authentication is temporarily unavailable.") from error

    if response.status_code in {400, 401, 422}:
        return False
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again later.")
    if response.status_code != 200:
        logger.error("Supabase authentication returned HTTP %s.", response.status_code)
        raise HTTPException(status_code=503, detail="Supabase authentication is temporarily unavailable.")

    try:
        authenticated_email = response.json()["user"]["email"]
    except (ValueError, KeyError, TypeError) as error:
        logger.error("Supabase authentication returned an invalid success response.")
        raise HTTPException(status_code=503, detail="Supabase returned an invalid authentication response.") from error
    return isinstance(authenticated_email, str) and authenticated_email.casefold() == email


def _class_payload(item: models.SchoolClass):
    return {
        "id": item.id, "name": item.name, "code": item.code, "teacher_id": item.teacher_id,
        "room": item.room, "schedule_days": item.schedule_days,
        "schedule_start": item.schedule_start.isoformat() if item.schedule_start else None,
        "schedule_end": item.schedule_end.isoformat() if item.schedule_end else None,
        "academic_term": item.academic_term, "active": item.active,
    }


def _student_payload(db: Session, student: models.Student, user: models.Teacher):
    class_ids_query = db.query(models.ClassEnrollment.class_id).join(
        models.SchoolClass, models.ClassEnrollment.class_id == models.SchoolClass.id
    ).filter(models.ClassEnrollment.student_id == student.id)
    if user.role != "admin":
        class_ids_query = class_ids_query.filter(models.SchoolClass.teacher_id == user.id)
    class_ids = [row[0] for row in class_ids_query.all()]
    records = repository.student_records(db, student.id, _teacher_scope(user))
    late_counts = _setting(db, "late_counts_as_attended", "true") == "true"
    return {
        "id": student.id, "name": student.name, "roll_no": student.roll_no,
        "email": student.email, "active": student.active, "class_ids": class_ids,
        "attendance_percent": repository.attendance_percent(records, late_counts),
        "face_registered": face_store.has_templates(student.id),
    }


def _student_by_san(db: Session, san: str) -> models.Student:
    normalized = san.strip().upper()
    if not normalized:
        raise HTTPException(status_code=422, detail="SAN/USN is required.")
    matches = db.query(models.Student).filter(
        func.upper(func.trim(models.Student.roll_no)) == normalized
    ).all()
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Multiple students match this SAN/USN after normalization. Resolve the duplicate identifiers first.",
        )
    if not matches:
        raise HTTPException(status_code=404, detail="Student not found for this SAN/USN.")
    return matches[0]


def _student_subject_payload(db: Session, student: models.Student):
    classes = db.query(models.SchoolClass).join(
        models.ClassEnrollment, models.ClassEnrollment.class_id == models.SchoolClass.id
    ).filter(
        models.ClassEnrollment.student_id == student.id
    ).order_by(models.SchoolClass.name).all()
    return {
        "student": {
            "id": student.id,
            "name": student.name,
            "san": student.roll_no,
            "email": student.email,
            "active": student.active,
        },
        "face_enrolled": face_store.has_templates(student.id),
        "subjects": [_class_payload(item) for item in classes],
    }


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/health/details")
def health_details(user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    return {
        "status": "ok",
        "database": db.bind.dialect.name if db.bind else "unknown",
        "active_admin_count": db.query(models.Teacher.id).filter_by(role="admin", active=True).count(),
        "face_models_loaded": engine.detector is not None and engine.recognizer is not None,
        "encryption_key_configured": bool(settings.encryption_key),
    }


@router.get("/auth/login")
def login_csrf(response: Response):
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        "csrf_token", token, httponly=False, secure=settings.secure_cookies,
        samesite="lax", max_age=8 * 60 * 60, path="/",
    )
    response.headers.update(PUBLIC_CACHE_HEADERS)
    return {"csrf_token": token}


@router.post("/auth/login")
def login(payload: schemas.LoginInput, request: Request, response: Response, db: Session = Depends(get_db)):
    security.record_login_ip(request.client.host if request.client else "unknown", db)
    email = str(payload.email).casefold()
    user = db.query(models.Teacher).filter(models.Teacher.email == email).first()
    now = datetime.now(timezone.utc)
    lock_time = user.locked_until if user is not None else None
    if lock_time is not None and lock_time.tzinfo is None:
        lock_time = lock_time.replace(tzinfo=timezone.utc)
    locked = lock_time is not None and lock_time > now
    supabase_password_ok = _supabase_authenticate(email, payload.password)
    if supabase_password_ok is None:
        password_ok = security.verify_password(
            user.password_hash if user else security._dummy_password_hash, payload.password
        )
    else:
        password_ok = supabase_password_ok

    if password_ok and user is None and supabase_password_ok is True:
        raise HTTPException(
            status_code=403,
            detail="Your Supabase account is not approved for this app. Ask an administrator to add your teacher profile.",
        )

    if user is None or not user.active or locked or not password_ok:
        if user is not None:
            security.audit(
                db, "failed_login", actor_id=user.id,
                detail=f"email={email}; locked={bool(locked)}",
                ip_address=request.client.host if request.client else None,
            )
            user.failed_attempts = user.failed_attempts + 1
            if user.failed_attempts >= 5:
                user.locked_until = now + timedelta(minutes=15)
                user.failed_attempts = 0
        db.commit()
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    user.failed_attempts = 0
    user.locked_until = None
    session_id = str(uuid.uuid4())
    db.add(models.LoginSession(
        id=session_id,
        teacher_id=user.id,
        session_version=user.session_version,
        expires_at=now + timedelta(hours=8),
    ))
    security.audit(db, "login", actor_id=user.id, ip_address=request.client.host if request.client else None)
    db.commit()
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        "attendance_session", security.issue_session(user, session_id), httponly=True,
        secure=settings.secure_cookies, samesite="lax", max_age=8 * 60 * 60, path="/",
    )
    response.set_cookie(
        "csrf_token", csrf, httponly=False, secure=settings.secure_cookies,
        samesite="lax", max_age=8 * 60 * 60, path="/",
    )
    return {"user": schemas.TeacherOutput.model_validate(user).model_dump(), "csrf_token": csrf}


@router.get("/auth/me")
def me(user: models.Teacher = Depends(security.current_user)):
    return schemas.TeacherOutput.model_validate(user).model_dump()


@router.post("/auth/logout")
def logout(request: Request, response: Response, user: models.Teacher = Depends(security.current_user),
           db: Session = Depends(get_db)):
    security.check_csrf(request)
    claims = security.decode_session(request.cookies["attendance_session"])
    login_session = db.get(models.LoginSession, claims["jti"])
    if login_session is not None:
        login_session.revoked_at = datetime.now(timezone.utc)
    _audit(request, db, user, "logout")
    db.commit()
    response.delete_cookie("attendance_session", path="/")
    response.delete_cookie("csrf_token", path="/")
    return {"ok": True}


@router.post("/auth/logout-everywhere")
def logout_everywhere(request: Request, response: Response, user: models.Teacher = Depends(security.current_user),
                      db: Session = Depends(get_db)):
    security.check_csrf(request)
    now = datetime.now(timezone.utc)
    db.query(models.LoginSession).filter_by(teacher_id=user.id, revoked_at=None).update(
        {models.LoginSession.revoked_at: now}, synchronize_session=False
    )
    user.session_version += 1
    _audit(request, db, user, "logout_everywhere")
    db.commit()
    response.delete_cookie("attendance_session", path="/")
    response.delete_cookie("csrf_token", path="/")
    return {"ok": True}


@router.post("/auth/password")
def change_password(payload: schemas.PasswordChange, request: Request,
                    user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    if not security.verify_password(user.password_hash, payload.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if security.password_is_weak(payload.new_password):
        raise HTTPException(status_code=422, detail="Choose a less common password with at least 10 characters.")
    user.password_hash = security.hash_password(payload.new_password)
    user.session_version += 1
    _audit(request, db, user, "password_changed", entity_type="teacher", entity_id=user.id)
    db.commit()
    return {"ok": True, "message": "Password changed. Log in again on your devices."}


@router.get("/dashboard")
def dashboard(user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    today = datetime.now().date()
    classes = repository.visible_classes(db, user)
    class_ids = [item.id for item in classes]
    grace = int(_setting(db, "grace_period_minutes", "10"))
    for session_row in db.query(models.ClassSession).filter(
        models.ClassSession.class_id.in_(class_ids or [-1]),
        models.ClassSession.status == "held",
    ).all():
        repository.ensure_attendance_records(db, session_row, grace)
    db.commit()
    today_sessions = db.query(models.ClassSession).filter(
        models.ClassSession.class_id.in_(class_ids or [-1]),
        models.ClassSession.session_date == today,
    ).all()
    held_sessions = db.query(models.ClassSession).filter(
        models.ClassSession.class_id.in_(class_ids or [-1]),
        models.ClassSession.status == "held",
    ).order_by(models.ClassSession.session_date).all()
    records = db.query(models.AttendanceRecord).join(models.ClassSession).filter(
        models.ClassSession.class_id.in_(class_ids or [-1]),
        models.ClassSession.status == "held",
    ).all()
    today_records = [
        row for row in records
        if db.get(models.ClassSession, row.session_id).session_date == today
    ]
    below = []
    student_ids = repository.visible_student_ids(db, user)
    students_query = db.query(models.Student).filter(models.Student.active.is_(True))
    if student_ids is not None:
        students_query = students_query.filter(models.Student.id.in_(student_ids or [-1]))
    threshold = float(_setting(db, "low_attendance_threshold", "75"))
    for student in students_query.all():
        percent = repository.attendance_percent(
            repository.student_records(db, student.id, _teacher_scope(user)),
            _setting(db, "late_counts_as_attended", "true") == "true",
        )
        if percent is not None and percent < threshold:
            below.append({"id": student.id, "name": student.name, "roll_no": student.roll_no, "percent": percent})
    events_query = db.query(models.AttendanceEvent)
    if student_ids is not None:
        events_query = events_query.join(
            models.ClassEnrollment, models.ClassEnrollment.student_id == models.AttendanceEvent.student_id
        ).join(
            models.SchoolClass, models.ClassEnrollment.class_id == models.SchoolClass.id
        ).filter(
            models.ClassEnrollment.student_id.in_(student_ids or [-1]),
            models.SchoolClass.teacher_id == user.id,
        ).distinct()
    recent_events = events_query.order_by(models.AttendanceEvent.timestamp.desc()).limit(12).all()
    late_counts = _setting(db, "late_counts_as_attended", "true") == "true"
    trend = []
    for session_row in held_sessions[-28:]:
        session_records = [row for row in records if row.session_id == session_row.id]
        trend.append({
            "date": session_row.session_date.isoformat(),
            "label": session_row.session_date.strftime("%b %d"),
            "attendance": repository.attendance_percent(session_records, late_counts),
        })
    class_averages = []
    for item in classes:
        class_records = [
            row for row in records
            if db.get(models.ClassSession, row.session_id).class_id == item.id
        ]
        class_averages.append({
            "name": item.name,
            "attendance": repository.attendance_percent(class_records, late_counts),
        })
    return {
        "today": today.isoformat(),
        "classes_today": [_class_payload(item) for item in classes if any(s.class_id == item.id for s in today_sessions)],
        "held_sessions": sum(s.status == "held" for s in today_sessions),
        "attendance_counts": {
            key: sum(record.status == key for record in today_records)
            for key in ("present", "late", "absent", "excused")
        },
        "overall_percent": repository.attendance_percent(records, late_counts),
        "attendance_trend": trend,
        "class_averages": class_averages,
        "below_threshold_count": len(below),
        "students_needing_attention": below[:10],
        "recent_events": [
            {"id": event.id, "student": db.get(models.Student, event.student_id).name,
             "direction": event.direction, "timestamp": event.timestamp.isoformat(), "camera_id": event.camera_id}
            for event in recent_events
            if db.get(models.Student, event.student_id) is not None
        ],
    }


@router.get("/classes")
def list_classes(user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    return [_class_payload(item) for item in repository.visible_classes(db, user)]


@router.post("/classes")
def create_class(payload: schemas.ClassInput, request: Request, user: models.Teacher = Depends(security.current_user),
                 db: Session = Depends(get_db)):
    security.check_csrf(request)
    teacher_id = payload.teacher_id if user.role == "admin" and payload.teacher_id else user.id
    if db.get(models.Teacher, teacher_id) is None:
        raise HTTPException(status_code=422, detail="Teacher not found.")
    item = models.SchoolClass(**payload.model_dump(exclude={"teacher_id"}), teacher_id=teacher_id)
    db.add(item)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Class code is already in use.") from error
    _audit(request, db, user, "class_created", entity_type="class", entity_id=item.id)
    db.commit()
    return _class_payload(item)


@router.patch("/classes/{class_id}")
def update_class(class_id: str, payload: schemas.ClassInput, request: Request,
                 user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    item = repository.get_class(db, user, class_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "teacher_id" and user.role != "admin":
            continue
        if key == "teacher_id" and value is None:
            continue
        setattr(item, key, value)
    _audit(request, db, user, "class_updated", entity_type="class", entity_id=item.id)
    db.commit()
    return _class_payload(item)


@router.delete("/classes/{class_id}")
def archive_class(class_id: str, request: Request, user: models.Teacher = Depends(security.current_user),
                  db: Session = Depends(get_db)):
    security.check_csrf(request)
    item = repository.get_class(db, user, class_id)
    item.active = False
    _audit(request, db, user, "class_archived", entity_type="class", entity_id=item.id)
    db.commit()
    return {"ok": True}


@router.get("/classes/{class_id}/detail")
def class_detail(class_id: str, user: models.Teacher = Depends(security.current_user),
                 db: Session = Depends(get_db)):
    item = repository.get_class(db, user, class_id)
    enrollment_rows = db.query(models.ClassEnrollment).filter_by(class_id=class_id).all()
    students = [db.get(models.Student, row.student_id) for row in enrollment_rows]
    sessions = db.query(models.ClassSession).filter_by(class_id=class_id).order_by(
        models.ClassSession.session_date.desc()
    ).all()
    grace = int(_setting(db, "grace_period_minutes", "10"))
    for session_row in sessions:
        if session_row.status == "held":
            repository.ensure_attendance_records(db, session_row, grace)
    db.commit()
    records = db.query(models.AttendanceRecord).join(
        models.ClassSession, models.ClassSession.id == models.AttendanceRecord.session_id
    ).filter(models.ClassSession.class_id == class_id).all()
    return {
        "class": _class_payload(item),
        "students": [_student_payload(db, student, user) for student in students if student and student.active],
        "sessions": [{"id": row.id, "date": row.session_date.isoformat(), "start_time": row.start_time.isoformat(),
                      "end_time": row.end_time.isoformat(), "status": row.status} for row in sessions],
        "attendance_records": [
            {"session_id": row.session_id, "student_id": row.student_id, "status": row.status,
             "first_in": row.first_in.isoformat() if row.first_in else None,
             "last_out": row.last_out.isoformat() if row.last_out else None,
             "minutes_present": row.minutes_present, "override_reason": row.override_reason}
            for row in records
        ],
    }


@router.post("/classes/{class_id}/students/{student_id}")
def add_student_to_class(class_id: str, student_id: str, request: Request,
                         user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    repository.get_class(db, user, class_id)
    if db.get(models.Student, student_id) is None:
        raise HTTPException(status_code=404, detail="Student not found.")
    if not db.query(models.ClassEnrollment).filter_by(class_id=class_id, student_id=student_id).first():
        db.add(models.ClassEnrollment(class_id=class_id, student_id=student_id))
        db.commit()
    return {"ok": True}


@router.post("/classes/{class_id}/students/bulk")
def bulk_add_students(class_id: str, payload: schemas.BulkEnrollInput, request: Request,
                      user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    repository.get_class(db, user, class_id)
    roll_numbers = list(dict.fromkeys(payload.roll_numbers))
    students = db.query(models.Student).filter(
        func.upper(func.trim(models.Student.roll_no)).in_(roll_numbers)
    ).all()
    by_roll = {}
    for student in students:
        normalized = student.roll_no.strip().upper()
        if normalized in by_roll:
            raise HTTPException(
                status_code=409,
                detail="Multiple students match a SAN/USN after normalization. Resolve the duplicate identifiers first.",
            )
        by_roll[normalized] = student
    missing = [roll for roll in roll_numbers if roll not in by_roll]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown roll numbers: {', '.join(missing[:20])}")
    added = 0
    for roll in roll_numbers:
        student = by_roll[roll]
        if not db.query(models.ClassEnrollment).filter_by(class_id=class_id, student_id=student.id).first():
            db.add(models.ClassEnrollment(class_id=class_id, student_id=student.id))
            added += 1
    db.commit()
    return {"added": added, "total": len(roll_numbers)}


@router.delete("/classes/{class_id}/students/{student_id}")
def remove_student_from_class(class_id: str, student_id: str, request: Request,
                              user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    repository.get_class(db, user, class_id)
    row = db.query(models.ClassEnrollment).filter_by(class_id=class_id, student_id=student_id).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.post("/classes/{class_id}/sessions")
def create_sessions(class_id: str, payload: schemas.SessionInput, request: Request,
                    user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    repository.get_class(db, user, class_id)
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=422, detail="Session end must be after its start.")
    created = []
    for week in range(payload.repeat_weeks):
        item = models.ClassSession(
            class_id=class_id, session_date=payload.session_date + timedelta(days=7 * week),
            start_time=payload.start_time, end_time=payload.end_time, status=payload.status,
        )
        db.add(item)
        db.flush()
        created.append(item)
    db.commit()
    return [{"id": item.id, "date": item.session_date.isoformat(), "status": item.status} for item in created]


@router.patch("/sessions/{session_id}")
def update_session(session_id: str, payload: schemas.SessionStatusInput, request: Request,
                   user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    item = db.get(models.ClassSession, session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    repository.get_class(db, user, item.class_id)
    item.status = payload.status
    if payload.status == "held":
        repository.ensure_attendance_records(
            db, item, int(_setting(db, "grace_period_minutes", "10"))
        )
    db.commit()
    return {"id": item.id, "status": item.status}


@router.get("/students")
def list_students(user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db),
                  q: str = "", sort: str = "name"):
    allowed = repository.visible_student_ids(db, user)
    query = db.query(models.Student).filter(models.Student.active.is_(True))
    if allowed is not None:
        query = query.filter(models.Student.id.in_(allowed or [-1]))
    if q:
        like = f"%{q[:100]}%"
        query = query.filter(or_(models.Student.name.ilike(like), models.Student.roll_no.ilike(like)))
    query = query.order_by(models.Student.roll_no if sort == "roll_no" else models.Student.name)
    return [_student_payload(db, student, user) for student in query.limit(500).all()]


@router.post("/students")
def create_student(payload: schemas.StudentInput, request: Request,
                   user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    if payload.class_ids and user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required to assign classes.")
    existing_san = db.query(models.Student.id).filter(
        func.upper(func.trim(models.Student.roll_no)) == payload.roll_no
    ).first()
    if existing_san:
        raise HTTPException(status_code=409, detail="SAN/USN is already registered.")
    student = models.Student(name=payload.name.strip(), roll_no=payload.roll_no.strip(), email=payload.email)
    db.add(student)
    try:
        db.flush()
        for class_id in payload.class_ids:
            repository.get_class(db, user, class_id)
            db.add(models.ClassEnrollment(student_id=student.id, class_id=class_id))
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Roll number is already in use.") from error
    _audit(request, db, user, "student_created", entity_type="student", entity_id=student.id)
    db.commit()
    return _student_payload(db, student, user)


@router.get("/students/lookup")
def lookup_student_by_san(san: str, user: models.Teacher = Depends(security.require_admin),
                           db: Session = Depends(get_db)):
    student = _student_by_san(db, san)
    return _student_subject_payload(db, student)


@router.post("/students/subjects")
def assign_student_subjects(payload: schemas.StudentSubjectAssignmentInput, request: Request,
                            user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    student = _student_by_san(db, payload.san)
    class_ids = list(dict.fromkeys(payload.class_ids))
    classes = db.query(models.SchoolClass).filter(
        models.SchoolClass.id.in_(class_ids), models.SchoolClass.active.is_(True)
    ).all()
    if len(classes) != len(class_ids):
        raise HTTPException(status_code=404, detail="One or more active classes were not found.")
    existing = {
        row[0] for row in db.query(models.ClassEnrollment.class_id).filter(
            models.ClassEnrollment.student_id == student.id,
            models.ClassEnrollment.class_id.in_(class_ids),
        ).all()
    }
    added = [class_id for class_id in class_ids if class_id not in existing]
    for class_id in added:
        db.add(models.ClassEnrollment(student_id=student.id, class_id=class_id))
    if added:
        _audit(request, db, user, "student_subjects_assigned", entity_type="student",
               entity_id=student.id, detail=f"added={len(added)}")
        db.commit()
    return _student_subject_payload(db, student)


@router.delete("/students/subjects")
def unassign_student_subject(san: str, class_id: str, request: Request,
                             user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    student = _student_by_san(db, san)
    if db.get(models.SchoolClass, class_id) is None:
        raise HTTPException(status_code=404, detail="Class not found.")
    enrollment = db.query(models.ClassEnrollment).filter_by(
        student_id=student.id, class_id=class_id
    ).first()
    if enrollment is not None:
        db.delete(enrollment)
        _audit(request, db, user, "student_subject_unassigned", entity_type="student",
               entity_id=student.id, detail="removed=1")
        db.commit()
    return _student_subject_payload(db, student)


@router.get("/students/{student_id}")
def student_detail(student_id: str, user: models.Teacher = Depends(security.current_user),
                   db: Session = Depends(get_db)):
    student = repository.get_student(db, user, student_id)
    enrollments = db.query(models.ClassEnrollment).filter_by(student_id=student_id).all()
    allowed_class_ids = []
    for enrollment in enrollments:
        class_row = db.get(models.SchoolClass, enrollment.class_id)
        if class_row and (user.role == "admin" or class_row.teacher_id == user.id):
            allowed_class_ids.append(class_row.id)
    grace = int(_setting(db, "grace_period_minutes", "10"))
    for session_row in db.query(models.ClassSession).filter(
        models.ClassSession.class_id.in_(allowed_class_ids or [-1]),
        models.ClassSession.status == "held",
    ).all():
        repository.ensure_attendance_records(db, session_row, grace)
    db.commit()
    records = repository.student_records(db, student_id, _teacher_scope(user))
    events_query = db.query(models.AttendanceEvent).filter_by(student_id=student_id)
    if user.role != "admin":
        events_query = events_query.join(
            models.ClassEnrollment, models.ClassEnrollment.student_id == models.AttendanceEvent.student_id
        ).join(
            models.SchoolClass, models.ClassEnrollment.class_id == models.SchoolClass.id
        ).filter(models.SchoolClass.teacher_id == user.id).distinct()
    events = events_query.order_by(models.AttendanceEvent.timestamp.desc()).limit(500).all()
    return {
        "student": _student_payload(db, student, user),
        "attendance_percent": repository.attendance_percent(
            records, _setting(db, "late_counts_as_attended", "true") == "true"
        ),
        "records": [
            {"session_id": row.session_id, "date": db.get(models.ClassSession, row.session_id).session_date.isoformat(),
             "status": row.status, "first_in": row.first_in.isoformat() if row.first_in else None,
             "last_out": row.last_out.isoformat() if row.last_out else None,
             "minutes_present": row.minutes_present, "override_reason": row.override_reason}
            for row in records
        ],
        "events": [{"direction": event.direction, "timestamp": event.timestamp.isoformat(),
                    "score": event.score, "camera_id": event.camera_id} for event in events],
        "absent_dates": [db.get(models.ClassSession, row.session_id).session_date.isoformat()
                         for row in records if row.status == "absent"],
        "face_registered": face_store.has_templates(student_id),
    }


@router.post("/sessions/{session_id}/students/{student_id}/override")
def override_attendance(session_id: str, student_id: str, payload: schemas.OverrideInput,
                        request: Request, user: models.Teacher = Depends(security.current_user),
                        db: Session = Depends(get_db)):
    security.check_csrf(request)
    session_row = db.get(models.ClassSession, session_id)
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    repository.get_class(db, user, session_row.class_id)
    enrollment = db.query(models.ClassEnrollment).filter_by(
        class_id=session_row.class_id, student_id=student_id
    ).first()
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Student is not enrolled in this class.")
    record = db.query(models.AttendanceRecord).filter_by(
        student_id=student_id, session_id=session_id
    ).first()
    if record is None:
        record = models.AttendanceRecord(student_id=student_id, session_id=session_id)
        db.add(record)
    record.status = payload.status
    record.overridden_by = user.id
    record.override_reason = payload.reason.strip()
    _audit(request, db, user, "attendance_override", entity_type="attendance_record",
           entity_id=record.id, detail=f"status={payload.status}; reason={payload.reason}")
    db.commit()
    return {"ok": True, "status": record.status}


@router.get("/settings")
def get_settings(user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    return {
        "grace_period_minutes": int(_setting(db, "grace_period_minutes", "10")),
        "late_counts_as_attended": _setting(db, "late_counts_as_attended", "true") == "true",
        "low_attendance_threshold": float(_setting(db, "low_attendance_threshold", "75")),
        "face_match_threshold": float(_setting(db, "face_match_threshold", str(DEFAULT_THRESHOLD))),
        "academic_term": _setting(db, "academic_term", ""),
        "data_retention_days": int(_setting(db, "data_retention_days", "365")),
        "store_profile_photo": _setting(db, "store_profile_photo", "false") == "true",
        "formula": "(present + late when enabled) / held enrolled sessions excluding excused",
    }


@router.put("/settings")
def update_settings(payload: schemas.SettingsInput, request: Request,
                    user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    for key, value in payload.model_dump().items():
        row = db.get(models.AppSetting, key)
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if row is None:
            db.add(models.AppSetting(key=key, value=text))
        else:
            row.value = text
    _audit(request, db, user, "settings_updated")
    db.commit()
    return {"ok": True}


@router.post("/settings/purge")
def purge_old_events(request: Request, user: models.Teacher = Depends(security.current_user),
                     db: Session = Depends(get_db)):
    security.check_csrf(request)
    days = int(_setting(db, "data_retention_days", "365"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    count = db.query(models.AttendanceEvent).filter(models.AttendanceEvent.timestamp < cutoff).delete(
        synchronize_session=False
    )
    _audit(request, db, user, "old_events_purged", detail=f"deleted={count}")
    db.commit()
    return {"deleted": count}


@router.get("/reports")
def reports(user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    students = db.query(models.Student).filter(models.Student.active.is_(True)).all()
    return {
        "students": [_student_payload(db, student, user) for student in students
                     if (allowed := repository.visible_student_ids(db, user)) is None or student.id in allowed],
        "classes": [_class_payload(item) for item in repository.visible_classes(db, user)],
    }


@router.get("/reports/export.csv")
def export_report(request: Request, user: models.Teacher = Depends(security.current_user),
                  db: Session = Depends(get_db)):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "roll_no", "attendance_percent", "face_registered"])
    students = db.query(models.Student).filter(models.Student.active.is_(True)).all()
    allowed = repository.visible_student_ids(db, user)
    for student in students:
        if allowed is not None and student.id not in allowed:
            continue
        data = _student_payload(db, student, user)
        writer.writerow([data["name"], data["roll_no"], data["attendance_percent"], data["face_registered"]])
    _audit(request, db, user, "report_exported", entity_type="report", detail="student attendance CSV")
    db.commit()
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=attendance-report.csv"})


@router.get("/teachers")
def list_teachers(user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    return [schemas.TeacherOutput.model_validate(item).model_dump()
            for item in db.query(models.Teacher).order_by(models.Teacher.name).all()]


@router.post("/teachers")
def create_teacher(payload: schemas.TeacherCreate, request: Request,
                   user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    if security.password_is_weak(payload.password):
        raise HTTPException(status_code=422, detail="Choose a less common password with at least 10 characters.")
    item = models.Teacher(
        name=payload.name.strip(), email=str(payload.email).casefold(),
        password_hash=security.hash_password(payload.password), role=payload.role,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email is already in use.") from error
    _audit(request, db, user, "teacher_created", entity_type="teacher", entity_id=item.id)
    db.commit()
    return schemas.TeacherOutput.model_validate(item).model_dump()


@router.patch("/teachers/{teacher_id}/active")
def set_teacher_active(teacher_id: str, active: bool, request: Request,
                       user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    target = db.get(models.Teacher, teacher_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Teacher not found.")
    if target.id == user.id and not active:
        raise HTTPException(status_code=422, detail="You cannot deactivate your own account.")
    target.active = active
    if not active:
        target.session_version += 1
    _audit(request, db, user, "teacher_status_changed", entity_type="teacher",
           entity_id=target.id, detail=f"active={active}")
    db.commit()
    return {"ok": True}


@router.post("/teachers/{teacher_id}/reset-password")
def reset_teacher_password(teacher_id: str, payload: schemas.PasswordReset, request: Request,
                           user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    password = payload.password
    if security.password_is_weak(password):
        raise HTTPException(status_code=422, detail="Choose a less common password with at least 10 characters.")
    target = db.get(models.Teacher, teacher_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Teacher not found.")
    target.password_hash = security.hash_password(password)
    target.session_version += 1
    _audit(request, db, user, "teacher_password_reset", entity_type="teacher", entity_id=target.id)
    db.commit()
    return {"ok": True}


@router.get("/audit")
def list_audit(user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    return [
        {"id": row.id, "actor": db.get(models.Teacher, row.actor_id).email if row.actor_id and db.get(models.Teacher, row.actor_id) else None,
         "action": row.action, "entity_type": row.entity_type, "entity_id": row.entity_id,
         "detail": row.detail, "ip_address": row.ip_address, "created_at": row.created_at.isoformat()}
        for row in db.query(models.AuditLog).order_by(models.AuditLog.created_at.desc()).limit(500).all()
    ]


@router.get("/devices")
def list_devices(user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    return [{"id": item.id, "name": item.name, "active": item.active, "created_at": item.created_at.isoformat()}
            for item in db.query(models.Device).order_by(models.Device.id).all()]


@router.post("/devices")
def create_device(payload: schemas.DeviceCreate, request: Request, user: models.Teacher = Depends(security.require_admin),
                 db: Session = Depends(get_db)):
    security.check_csrf(request)
    if not settings.device_api_key_pepper:
        raise HTTPException(status_code=503, detail="DEVICE_API_KEY_PEPPER must be configured.")
    key = secrets.token_urlsafe(32)
    item = models.Device(name=payload.name.strip(), api_key_hash=security.device_key_hash(key))
    db.add(item)
    db.flush()
    _audit(request, db, user, "device_created", entity_type="device", entity_id=item.id)
    db.commit()
    return {"id": item.id, "name": item.name, "api_key": key, "warning": "Copy this key now; it will not be shown again."}


@router.post("/devices/{device_id}/active")
def set_device_active(device_id: str, active: bool, request: Request,
                      user: models.Teacher = Depends(security.require_admin), db: Session = Depends(get_db)):
    security.check_csrf(request)
    item = db.get(models.Device, device_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    item.active = active
    db.commit()
    return {"ok": True}


@router.post("/device/events")
def device_event(payload: schemas.DeviceEventInput, device: models.Device = Depends(security.require_device),
                 db: Session = Depends(get_db)):
    if (payload.student_id is None) == (payload.student_roll_no is None):
        raise HTTPException(status_code=422, detail="Provide exactly one student identifier.")
    student = (
        db.get(models.Student, payload.student_id)
        if payload.student_id is not None
        else _student_by_san(db, payload.student_roll_no)
    )
    if student is None or not student.active:
        raise HTTPException(status_code=404, detail="Student not found.")
    stamp = payload.timestamp
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    duplicate = db.query(models.AttendanceEvent).filter(
        models.AttendanceEvent.student_id == student.id,
        models.AttendanceEvent.direction == payload.direction,
        models.AttendanceEvent.timestamp >= stamp - timedelta(seconds=30),
        models.AttendanceEvent.timestamp <= stamp + timedelta(seconds=30),
    ).first()
    if duplicate:
        return {"ok": True, "duplicate": True, "event_id": duplicate.id}
    event = models.AttendanceEvent(
        student_id=student.id, direction=payload.direction, timestamp=stamp,
        score=payload.score, camera_id=payload.camera_id or device.name, source="door",
    )
    db.add(event)
    for enrollment in db.query(models.ClassEnrollment).filter_by(student_id=student.id).all():
        sessions = db.query(models.ClassSession).filter(
            models.ClassSession.class_id == enrollment.class_id,
            models.ClassSession.session_date == stamp.date(),
            models.ClassSession.status == "held",
        ).all()
        for session in sessions:
            repository.ensure_attendance_records(
                db, session, int(db.get(models.AppSetting, "grace_period_minutes").value)
                if db.get(models.AppSetting, "grace_period_minutes") else 10,
            )
    db.commit()
    return {"ok": True, "duplicate": False, "event_id": event.id}


@router.get("/device/embeddings")
def device_embeddings(device: models.Device = Depends(security.require_device), db: Session = Depends(get_db)):
    # This intentionally device-authenticated export is for local matching only.
    # It must never be available through a teacher-facing route.
    students = {
        student.id: student
        for student in db.query(models.Student).filter(
            models.Student.active.is_(True),
            models.Student.consent_given_at.is_not(None),
        ).all()
    }
    values = []
    for row in face_store.list_templates():
        if row.student_id in students:
            values.append({
                "student_id": row.student_id,
                "vector": _decode_vector(row.encrypted_vector).tolist(),
            })
    return {"model_version": "sface_2021dec", "students": values}


@router.post("/face/verify")
def verify_face(payload: schemas.FaceVerificationInput, request: Request,
                user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    """Verify eight browser camera frames and record attendance only after a positive decision."""
    security.check_csrf(request)
    if engine.detector is None or engine.recognizer is None:
        _ensure_face_engine()

    students_query = db.query(models.Student).filter(
        models.Student.active.is_(True),
        models.Student.consent_given_at.is_not(None),
    )
    visible_ids = repository.visible_student_ids(db, user)
    if visible_ids is not None:
        students_query = students_query.filter(models.Student.id.in_(visible_ids))

    students = {student.id: student for student in students_query.all()}
    enrollments = {}
    identities = {}
    for embedding in face_store.list_templates():
        student = students.get(embedding.student_id)
        if student is None or embedding.model_version != "sface_2021dec":
            continue
        enrollments.setdefault(student.id, []).append(_decode_vector(embedding.encrypted_vector))
        identities[student.id] = {"name": student.name, "roll_no": student.roll_no}
    if not enrollments:
        raise HTTPException(
            status_code=409,
            detail="No consented, completed face enrollments are available to verify.",
        )

    try:
        threshold = float(_setting(db, "face_match_threshold", str(DEFAULT_THRESHOLD)))
    except ValueError as error:
        raise HTTPException(status_code=500, detail="The configured face-match threshold is invalid.") from error
    if not 0.0 <= threshold <= 1.0:
        raise HTTPException(status_code=500, detail="The configured face-match threshold is invalid.")

    frame_results = []
    decision_scores = []
    frame_issues = []
    for index, encoded in enumerate(payload.frames, start=1):
        frame = _decode_camera_frame(encoded)
        try:
            faces = engine.detect(frame)
        except cv2.error as error:
            logger.exception("YuNet detection failed during verification.")
            raise HTTPException(status_code=503, detail="Face detection failed. Please retry the scan.") from error
        if len(faces) != 1:
            frame_issues.append({"frame": index, "reason": "exactly_one_face_required"})
            continue
        face = faces[0]
        good, reason, _ = engine.quality(frame, face)
        if not good:
            frame_issues.append({"frame": index, "reason": reason or "low_quality"})
            continue
        probe = engine.align_and_embed(frame, face)
        scores = score_embedding(probe, enrollments)
        vote, best_score, margin, vote_reason = frame_vote(scores, threshold)
        decision_scores.append(scores)
        frame_results.append({
            "frame": index,
            "scores": [
                {
                    "name": identities[identity]["name"],
                    "roll_no": identities[identity]["roll_no"],
                    "score": score,
                }
                for identity, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
            ],
            "vote": identities[vote]["name"] if vote is not None else None,
            "best_score": best_score,
            "margin": margin if np.isfinite(margin) else None,
            "reason": vote_reason,
        })

    if frame_issues or len(frame_results) != 8:
        return {
            "accepted": False,
            "clear_scan": False,
            "message": "Can't get a clear scan, try again. Keep one face centered, steady, and well lit.",
            "threshold": threshold,
            "good_frames": len(frame_results),
            "frame_issues": frame_issues,
            "frame_results": frame_results,
        }

    decision = decide_votes(decision_scores, threshold)
    votes = {identity: decision["votes"].get(identity, 0) for identity in identities}
    vote_summary = [
        {"name": identities[identity]["name"], "roll_no": identities[identity]["roll_no"], "votes": count}
        for identity, count in sorted(votes.items(), key=lambda item: item[1], reverse=True)
    ]
    response = {
        "accepted": decision["accepted"],
        "clear_scan": True,
        "message": "",
        "threshold": threshold,
        "good_frames": len(frame_results),
        "frame_results": frame_results,
        "votes": vote_summary,
        "event_id": None,
        "duplicate_event": False,
    }
    if not decision["accepted"]:
        response["message"] = "NOT RECOGNIZED. The identity did not pass the frame vote and similarity checks."
        return response

    student_id = decision["identity"]
    student = db.get(models.Student, student_id)
    if student is None or not student.active or student.consent_given_at is None:
        response["accepted"] = False
        response["message"] = "NOT RECOGNIZED. The enrolled identity is no longer active."
        return response

    now = datetime.now(timezone.utc)
    average_score = float(np.mean([scores[student_id] for scores in decision_scores]))
    duplicate = db.query(models.AttendanceEvent).filter(
        models.AttendanceEvent.student_id == student.id,
        models.AttendanceEvent.direction == payload.direction,
        models.AttendanceEvent.timestamp >= now - timedelta(seconds=30),
        models.AttendanceEvent.timestamp <= now + timedelta(seconds=30),
    ).first()
    if duplicate is not None:
        event = duplicate
        response["duplicate_event"] = True
    else:
        event = models.AttendanceEvent(
            student_id=student.id,
            direction=payload.direction,
            timestamp=now,
            score=average_score,
            camera_id=payload.camera_id or "teacher-webcam",
            source="teacher_scan",
        )
        db.add(event)
        for enrollment in db.query(models.ClassEnrollment).filter_by(student_id=student.id).all():
            sessions = db.query(models.ClassSession).filter(
                models.ClassSession.class_id == enrollment.class_id,
                models.ClassSession.session_date == now.date(),
                models.ClassSession.status == "held",
            ).all()
            for session in sessions:
                repository.ensure_attendance_records(
                    db, session, int(_setting(db, "grace_period_minutes", "10"))
                )
    response["event_id"] = event.id
    response["message"] = f"SAME PERSON: {student.name} ({student.roll_no}), {payload.direction} scan recorded."
    _audit(
        request, db, user, "face_verification",
        entity_type="student", entity_id=student.id,
        detail=f"direction={payload.direction}; score={average_score:.4f}",
    )
    db.commit()
    return response


@router.post("/face/enrollments")
def start_face_enrollment(payload: schemas.ConsentInput, request: Request,
                          user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    if not payload.consent:
        raise HTTPException(status_code=422, detail="Student consent is required before face enrollment.")
    san = payload.roll_no
    matches = db.query(models.Student).filter(
        func.upper(func.trim(models.Student.roll_no)) == san
    ).all()
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Multiple students match this SAN/USN after normalization. Resolve the duplicate identifiers first.",
        )
    student = matches[0] if matches else None
    visible_student_ids = repository.visible_student_ids(db, user)
    if student and visible_student_ids is not None and student.id not in visible_student_ids \
            and student.consent_recorded_by != user.id:
        raise HTTPException(status_code=404, detail="Student not found.")
    if student and student.name.strip().casefold() != payload.name.strip().casefold():
        raise HTTPException(status_code=409, detail="This SAN/USN is already registered to a different student name.")
    if student is None:
        student = models.Student(name=payload.name.strip(), roll_no=san)
        db.add(student)
    student.roll_no = san
    student.name, student.email, student.active = payload.name.strip(), payload.email, True
    student.consent_given_at = datetime.now(timezone.utc)
    student.consent_note = payload.consent_note
    student.consent_recorded_by = user.id
    session_id = str(uuid.uuid4())
    state = new_state()
    now_epoch = datetime.now(timezone.utc).timestamp()
    state["attempt_started_at"] = now_epoch
    state["neutral_started_at"] = now_epoch
    state["self_test_vectors"] = []
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="SAN/USN is already registered.") from error
    face_store.create_session(session_id, user.id, student.id, state, expires_at)
    _audit(request, db, user, "face_enrollment_started", entity_type="student", entity_id=student.id)
    db.commit()
    return {"session_id": session_id, "student_id": student.id, "counts": state["counts"],
            "attempt": 1, "message": "Consent recorded; follow the measured-pose guidance."}


@router.delete("/face/enrollments/{session_id}")
def cancel_face_enrollment(session_id: str, request: Request,
                           user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    session = face_store.get_session(session_id)
    if session is None or session.teacher_id != user.id:
        raise HTTPException(status_code=404, detail="Enrollment session not found.")
    face_store.delete_session(session_id)
    _audit(request, db, user, "face_enrollment_cancelled", entity_type="student", entity_id=session.student_id)
    db.commit()
    return {"status": "cancelled"}


@router.post("/face/enrollments/{session_id}/frame")
@face_timing.timed_frame_endpoint(1)
def enrollment_frame(session_id: str, payload: schemas.EnrollmentFrameInput, request: Request,
                     user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    session = face_store.get_session(session_id)
    if session is None or session.teacher_id != user.id or session.completed:
        face_timing.reject("session_missing")
        raise HTTPException(status_code=404, detail="Enrollment session not found.")
    expiry = session.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < datetime.now(timezone.utc):
        face_timing.reject("session_expired")
        face_store.delete_session(session_id)
        db.commit()
        raise HTTPException(status_code=410, detail="Enrollment session expired.")
    try:
        import cv2
        with face_timing.measure("jpeg_decode"):
            raw = base64.b64decode(payload.frame, validate=True)
            frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    except (ValueError, cv2.error) as error:
        face_timing.reject("invalid_frame")
        raise HTTPException(status_code=422, detail="Frame must be a valid encoded image.") from error
    if frame is None:
        face_timing.reject("invalid_frame")
        raise HTTPException(status_code=422, detail="Frame must be a valid encoded image.")
    with face_timing.measure("model_check_load"):
        _ensure_face_engine()
    state = json.loads(session.state_json)
    now_epoch = datetime.now(timezone.utc).timestamp()
    attempt_started = float(state.setdefault("attempt_started_at", now_epoch))
    if now_epoch - attempt_started >= 55.0 and not is_complete(state):
        face_timing.reject("attempt_timeout")
        state["attempt"] += 1
        state["stable_pose"] = None
        state["stable_count"] = 0
        missing = [pose for pose, count in state["counts"].items() if count < 3]
        if state["attempt"] > 3:
            face_store.delete_session(session_id)
            _audit(request, db, user, "face_enrollment_failed", entity_type="student",
                   entity_id=session.student_id, detail=f"timeout; missing={','.join(missing)}")
            db.commit()
            return {"status": "failed", "message": f"Enrollment failed; missing poses: {', '.join(missing)}",
                    "counts": state["counts"]}
        state["attempt_started_at"] = now_epoch
        session.state_json = json.dumps(state)
        db.commit()
        return {"status": "retry", "message": f"Missing: {', '.join(missing)}. Continuing attempt {state['attempt']} of 3.",
                "counts": state["counts"], "attempt": state["attempt"]}
    with face_timing.measure("yunet_detection"):
        faces = engine.detect(frame)
    if len(faces) != 1:
        face_timing.reject("no_face" if not faces else "multiple_faces")
        state["stable_pose"] = None
        state["stable_count"] = 0
        session.state_json = json.dumps(state)
        db.commit()
        return {"status": "guiding", "message": "Only one face please" if faces else "Position your face in the circle",
                "counts": state["counts"]}
    face = faces[0]
    attempt = min(state["attempt"], 3)
    score_gate = {1: 0.7, 2: 0.6, 3: 0.5}[attempt]
    sharpness_floor = 0.0
    sharpness_samples = state["sharpness_samples"]
    if len(sharpness_samples) >= 10:
        sharpness_floor = max(
            1.0,
            float(np.median(sharpness_samples)) * {1: 0.4, 2: 0.3, 3: 0.2}[attempt],
        )
    with face_timing.measure("quality_checks"):
        good, reason, sharpness = engine.quality(frame, face, score_gate, sharpness_floor)
    if not good:
        face_timing.reject(reason if reason in face_timing.REJECTION_CODES else "invalid_pose")
        state["stable_pose"] = None
        state["stable_count"] = 0
        session.state_json = json.dumps(state)
        db.commit()
        labels = {"low_score": "Improve lighting", "too_small": "Move closer", "too_large": "Move back",
                  "off_center": "Center your face", "blurry": "Hold still"}
        return {"status": "guiding", "message": labels.get(reason, "Hold still"),
                "counts": state["counts"]}
    if len(sharpness_samples) < 10:
        sharpness_samples.append(float(sharpness))
    center = [float(face[0] + face[2] / 2), float(face[1] + face[3] / 2)]
    previous_center = state.get("previous_center")
    if previous_center and float(np.linalg.norm(np.asarray(center) - np.asarray(previous_center))) > float(face[2]) * 0.15:
        face_timing.reject("moving")
        state["previous_center"] = center
        state["stable_pose"] = None
        state["stable_count"] = 0
        session.state_json = json.dumps(state)
        db.commit()
        return {"status": "guiding", "message": "Hold still", "counts": state["counts"]}
    state["previous_center"] = center
    try:
        with face_timing.measure("solvepnp_pose"):
            yaw, pitch, _ = engine.estimate_pose(face, (frame.shape[1], frame.shape[0]))
    except (ValueError, cv2.error) as error:
        face_timing.reject("invalid_pose")
        return {"status": "guiding", "message": str(error)[:120],
                "counts": state["counts"]}
    calibration_started = float(state.setdefault("neutral_started_at", now_epoch))
    if state["neutral_offset"] is None and now_epoch - calibration_started < 1.0:
        state["neutral_samples"].append([yaw, pitch])
        session.state_json = json.dumps(state)
        db.commit()
        return {"status": "calibrating", "message": "Hold still; calibrating your neutral pose",
                "counts": state["counts"]}
    if state["neutral_offset"] is None:
        samples = np.asarray(state["neutral_samples"] or [[yaw, pitch]], dtype=np.float64)
        reference = float(samples[0, 0])
        unwrapped_yaws = [reference + ((value[0] - reference + 180) % 360 - 180) for value in samples]
        state["neutral_offset"] = [
            float(np.median(unwrapped_yaws)),
            float(np.median(samples[:, 1])),
        ]
    neutral_yaw, neutral_pitch = state["neutral_offset"]
    yaw = (yaw - neutral_yaw + 180) % 360 - 180
    pitch -= neutral_pitch
    state["pose_history"].append([yaw, pitch])
    state["pose_history"] = state["pose_history"][-5:]
    smoothed = np.median(np.asarray(state["pose_history"]), axis=0)
    yaw, pitch = float(smoothed[0]), float(smoothed[1])
    with face_timing.measure("pose_classification_gate"):
        pose = classify_pose(yaw, pitch)
        captured = observe(state, pose, True, now_epoch)
    if captured:
        try:
            # SFace alignment is the expensive part of the scan. Run it only
            # after the stable-pose gate has selected a frame for retention.
            with face_timing.measure("sface_embedding"):
                vector = engine.align_and_embed(frame, face)
        except (ValueError, cv2.error) as error:
            face_timing.reject("invalid_pose")
            state["counts"][pose] -= 1
            state["stable_pose"] = None
            state["stable_count"] = 0
            session.state_json = json.dumps(state)
            db.commit()
            return {"status": "guiding", "message": str(error)[:120],
                    "counts": state["counts"]}
        existing = face_store.list_session_embeddings(session_id, session.student_id, pose)
        for row in existing:
            if float(np.dot(vector, _decode_vector(row.encrypted_vector))) >= 0.95:
                face_timing.reject("duplicate")
                state["counts"][pose] -= 1
                session.state_json = json.dumps(state)
                db.commit()
                return {"status": "guiding", "message": "Hold a slightly different pose",
                        "counts": state["counts"], "pose": pose, "yaw": yaw, "pitch": pitch}
        with face_timing.measure("embedding_encrypt"):
            encrypted_vector = _encrypt_vector(vector)
        face_store.add_session_embedding(session_id, FaceVectorRecord(
            id=str(uuid.uuid4()),
            student_id=session.student_id,
            enrollment_session_id=session_id,
            pose=pose,
            encrypted_vector=encrypted_vector,
            model_version="sface_2021dec",
            quality=float(face[14]),
            yaw=yaw,
            pitch=pitch,
            created_at=datetime.now(timezone.utc),
        ))
        face_timing.accepted(pose)
    elif pose is None:
        face_timing.reject("invalid_pose")
    else:
        face_timing.reject("stable_wait")
    session.state_json = json.dumps(state)
    completed = is_complete(state)
    message = "Checking..." if completed else _prompt(state)
    db.commit()
    return {
        "status": "checking" if completed else "capturing",
        "message": message,
        "counts": state["counts"],
        "pose": pose,
        "yaw": round(yaw, 1),
        "pitch": round(pitch, 1),
        "captured": captured,
        "complete": False,
    }


def _prompt(state):
    directions = ("CENTER", "LEFT", "RIGHT", "UP", "DOWN", "UP-LEFT", "UP-RIGHT", "DOWN-LEFT", "DOWN-RIGHT")
    next_pose = next((name for name in directions if state["counts"][name] < 3), "CENTER")
    return {
        "CENTER": "Look straight at the camera", "LEFT": "Slowly turn your head to the left",
        "RIGHT": "Slowly turn your head to the right", "UP": "Look up",
        "DOWN": "Look down", "UP-LEFT": "Look up and left", "UP-RIGHT": "Look up and right",
        "DOWN-LEFT": "Look down and left", "DOWN-RIGHT": "Look down and right",
    }[next_pose]


@router.post("/face/enrollments/{session_id}/self-test")
@face_timing.timed_frame_endpoint(2)
def enrollment_self_test(session_id: str, payload: schemas.EnrollmentFrameInput, request: Request,
                         user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    session = face_store.get_session(session_id)
    if session is None or session.teacher_id != user.id or session.completed:
        face_timing.reject("session_missing")
        raise HTTPException(status_code=404, detail="Enrollment session not found.")
    expiry = session.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < datetime.now(timezone.utc):
        face_timing.reject("session_expired")
        face_store.delete_session(session_id)
        db.commit()
        raise HTTPException(status_code=410, detail="Enrollment session expired.")
    state = json.loads(session.state_json)
    if not is_complete(state):
        raise HTTPException(status_code=409, detail="All nine poses must be captured before self-test.")
    encoded_vectors = state.setdefault("self_test_vectors", [])
    try:
        import cv2
        with face_timing.measure("jpeg_decode"):
            frame = cv2.imdecode(np.frombuffer(base64.b64decode(payload.frame, validate=True), np.uint8), cv2.IMREAD_COLOR)
    except (ValueError, cv2.error) as error:
        face_timing.reject("invalid_frame")
        raise HTTPException(status_code=422, detail="Frame must be a valid encoded image.") from error
    if frame is None:
        face_timing.reject("invalid_frame")
        raise HTTPException(status_code=422, detail="Frame must be a valid encoded image.")
    with face_timing.measure("model_check_load"):
        _ensure_face_engine()
    with face_timing.measure("yunet_detection"):
        faces = engine.detect(frame)
    if len(faces) != 1:
        face_timing.reject("no_face" if not faces else "multiple_faces")
        return {"status": "checking", "message": "Look straight at the camera",
                "self_test_frames": len(encoded_vectors)}
    face = faces[0]
    with face_timing.measure("quality_checks"):
        good, quality_reason, _ = engine.quality(frame, face)
    if not good:
        face_timing.reject("self_test_quality" if quality_reason not in face_timing.REJECTION_CODES else quality_reason)
        return {"status": "checking", "message": "Hold still in the center",
                "self_test_frames": len(encoded_vectors)}
    with face_timing.measure("solvepnp_pose"):
        yaw, pitch, _ = engine.estimate_pose(face, (frame.shape[1], frame.shape[0]))
    neutral = state.get("neutral_offset")
    if neutral:
        yaw = (yaw - neutral[0] + 180) % 360 - 180
        pitch -= neutral[1]
    if abs(yaw) > 8 or abs(pitch) > 8:
        face_timing.reject("self_test_not_centered")
        state["self_test_vectors"] = []
        session.state_json = json.dumps(state)
        db.commit()
        return {"status": "checking", "message": "Look straight at the camera", "self_test_frames": 0}
    with face_timing.measure("sface_embedding"):
        vector = engine.align_and_embed(frame, face)
    if len(encoded_vectors) < 5:
        with face_timing.measure("self_test_embedding_encrypt"):
            encoded_vectors.append(_encode_transient_vector(vector))
        session.state_json = json.dumps(state)
        db.commit()
    vectors = [_decode_transient_vector(value) for value in encoded_vectors]
    if len(vectors) < 5:
        return {"status": "checking", "message": "Checking...", "self_test_frames": len(vectors)}
    stored = face_store.list_session_embeddings(session_id, session.student_id)
    vectors_by_pose = {}
    for row in stored:
        vectors_by_pose.setdefault(row.pose, []).append(_decode_vector(row.encrypted_vector))
    with face_timing.measure("embedding_consistency"):
        cohesion_ok, filtered, cohesion, reason = verify_embedding_quality(vectors_by_pose)
    if cohesion_ok:
        retained_rows = _retained_embedding_rows(stored, filtered)
        yaw_values = [row.yaw for row in retained_rows if row.yaw is not None]
        pitch_values = [row.pitch for row in retained_rows if row.pitch is not None]
        yaw_range = max(yaw_values) - min(yaw_values) if yaw_values else 0.0
        pitch_range = max(pitch_values) - min(pitch_values) if pitch_values else 0.0
        missing_range_poses = []
        if yaw_range < 40:
            missing_range_poses.extend(("LEFT", "RIGHT"))
        if pitch_range < 20:
            missing_range_poses.extend(("UP", "DOWN"))
        if missing_range_poses:
            face_timing.reject("invalid_pose")
            state["attempt"] += 1
            state["self_test_vectors"] = []
            if state["attempt"] > 3:
                face_store.delete_session(session_id)
                _audit(request, db, user, "face_enrollment_failed", entity_type="student",
                       entity_id=session.student_id,
                       detail=f"pose spread failed; yaw={yaw_range:.1f}; pitch={pitch_range:.1f}")
                db.commit()
                return {"status": "failed", "message": "Enrollment failed after three attempts; pose coverage was too narrow.",
                        "missing_poses": missing_range_poses, "counts": state["counts"]}
            for pose, kept in filtered.items():
                state["counts"][pose] = len(kept)
            for pose in missing_range_poses:
                rows = sorted(
                    face_store.list_session_embeddings(session_id, session.student_id, pose),
                    key=lambda row: row.id,
                )
                if len(rows) >= 3:
                    face_store.delete_session_embedding(session_id, rows[0].id)
                    state["counts"][pose] = 2
            session.state_json = json.dumps(state)
            state["attempt_started_at"] = datetime.now(timezone.utc).timestamp()
            session.state_json = json.dumps(state)
            _audit(request, db, user, "face_enrollment_retry", entity_type="student",
                   entity_id=session.student_id,
                   detail=f"pose spread too narrow; yaw={yaw_range:.1f}; pitch={pitch_range:.1f}")
            db.commit()
            return {"status": "retry", "message": "Turn further to capture the missing pose spread.",
                    "missing_poses": missing_range_poses, "yaw_range": round(yaw_range, 1),
                    "pitch_range": round(pitch_range, 1), "counts": state["counts"]}
    enrolled = [vector for pose_rows in filtered.values() for vector in pose_rows]
    probe = np.mean(vectors, axis=0)
    norm = float(np.linalg.norm(probe))
    score = float(np.max(np.asarray(enrolled) @ (probe / norm))) if norm and enrolled else 0.0
    if not cohesion_ok or score < 0.5:
        face_timing.reject("self_test_similarity")
        state["self_test_vectors"] = []
        state["self_test_failures"] += 1
        state["attempt"] += 1
        if state["attempt"] > 3:
            face_store.delete_session(session_id)
            _audit(request, db, user, "face_enrollment_failed", entity_type="student",
                   entity_id=session.student_id, detail=f"{reason or 'self-test failed'}; score={score:.3f}")
            db.commit()
            return {"status": "failed", "message": "Enrollment failed after three verification attempts; no face data was saved.",
                    "reason": reason or "Live self-test did not match.", "score": round(score, 4),
                    "counts": state["counts"]}
        state["attempt_started_at"] = datetime.now(timezone.utc).timestamp()
        for pose in state["counts"]:
            rows = sorted(
                face_store.list_session_embeddings(session_id, session.student_id, pose),
                key=lambda row: row.id,
            )
            if len(rows) >= 3:
                face_store.delete_session_embedding(session_id, rows[0].id)
                state["counts"][pose] = 2
        session.state_json = json.dumps(state)
        _audit(request, db, user, "face_enrollment_failed", entity_type="student",
               entity_id=session.student_id, detail=f"{reason or 'self-test failed'}; score={score:.3f}")
        db.commit()
        return {"status": "retry", "message": "Face data could not be verified; retry enrollment.",
                "reason": reason or "Live self-test did not match.", "score": round(score, 4),
                "counts": state["counts"]}
    # Refuse to attach a fresh enrollment to a face that strongly matches another student.
    with face_timing.measure("duplicate_identity_check"):
        for existing_student in db.query(models.Student).filter(
            models.Student.active.is_(True),
            models.Student.id != session.student_id,
            models.Student.consent_given_at.is_not(None),
        ).all():
            existing_vectors = face_store.list_templates(existing_student.id)
            if not existing_vectors:
                continue
            best_score = max(
                float(np.dot(vector, _decode_vector(row.encrypted_vector)))
                for vector in enrolled for row in existing_vectors
            )
            if best_score >= 0.65:
                face_timing.reject("duplicate_identity")
                face_store.delete_session(session_id)
                _audit(request, db, user, "face_duplicate_blocked", entity_type="student",
                       entity_id=session.student_id,
                       detail=f"possible duplicate student_id={existing_student.id}; score={best_score:.3f}")
                db.commit()
                return {
                    "status": "blocked",
                    "message": "Face data strongly matches another registered student.",
                    "possible_match": {"name": existing_student.name, "roll_no": existing_student.roll_no},
                    "score": round(best_score, 4),
                }
    pose_ranges = {}
    for pose in state["counts"]:
        pose_rows = [row for row in retained_rows if row.pose == pose]
        yaws = [row.yaw for row in pose_rows if row.yaw is not None]
        pitches = [row.pitch for row in pose_rows if row.pitch is not None]
        pose_ranges[pose] = {
            "count": len(pose_rows),
            "yaw": [round(min(yaws), 1), round(max(yaws), 1)] if yaws else None,
            "pitch": [round(min(pitches), 1), round(max(pitches), 1)] if pitches else None,
        }
        logger.info(
            "Enrollment coverage %s: %d embeddings; yaw=%s; pitch=%s",
            pose, len(pose_rows), pose_ranges[pose]["yaw"], pose_ranges[pose]["pitch"],
        )
    # Replace templates and remove enrollment state in one atomic vault update.
    face_store.replace_templates(session.student_id, retained_rows, session_id=session_id)
    state["self_test_vectors"] = []
    _audit(request, db, user, "face_enrollment", entity_type="student", entity_id=session.student_id,
           detail=f"9-pose coverage passed; yaw_range={yaw_range:.1f}; pitch_range={pitch_range:.1f}; "
                  f"cohesion={cohesion:.3f}; self-test={score:.3f}")
    db.commit()
    return {"status": "complete", "message": "Face scan complete", "score": round(score, 4),
            "cohesion": round(cohesion or 0, 4), "counts": state["counts"],
            "pose_ranges": pose_ranges}


@router.get("/faces")
def registered_faces(user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    students = db.query(models.Student).filter(models.Student.active.is_(True)).all()
    visible = repository.visible_student_ids(db, user)
    output = []
    for student in students:
        if visible is not None and student.id not in visible:
            continue
        rows = face_store.list_templates(student.id)
        if rows:
            output.append({
                "student_id": student.id, "name": student.name, "roll_no": student.roll_no,
                "enrolled_at": min(row.created_at for row in rows).isoformat(),
                "pose_counts": {pose: sum(row.pose == pose for row in rows)
                                for pose in ("CENTER", "LEFT", "RIGHT", "UP", "DOWN",
                                             "UP-LEFT", "UP-RIGHT", "DOWN-LEFT", "DOWN-RIGHT")},
                "embedding_count": len(rows),
                "quality": round(sum(row.quality or 0 for row in rows) / len(rows), 3),
            })
    return output


@router.delete("/students/{student_id}/face")
def delete_face(student_id: str, confirm: str, request: Request,
                user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    student = repository.get_student(db, user, student_id)
    if confirm != student.roll_no:
        raise HTTPException(status_code=422, detail="Typed confirmation must match the student roll number.")
    face_store.delete_templates(student_id)
    _audit(request, db, user, "face_data_deleted", entity_type="student", entity_id=student_id)
    db.commit()
    return {"ok": True}


@router.post("/students/{student_id}/withdraw-consent")
def withdraw_consent(student_id: str, request: Request,
                     user: models.Teacher = Depends(security.current_user), db: Session = Depends(get_db)):
    security.check_csrf(request)
    student = repository.get_student(db, user, student_id)
    student.consent_given_at = None
    student.consent_note = None
    face_store.delete_templates(student_id)
    _audit(request, db, user, "consent_withdrawn", entity_type="student", entity_id=student_id)
    db.commit()
    return {"ok": True}
