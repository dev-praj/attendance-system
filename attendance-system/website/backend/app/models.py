"""SQLAlchemy data model; schema changes are managed with Alembic migrations."""

from datetime import date, datetime, time, timezone
import uuid

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, Uuid,
    Time, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utc_now():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


class Teacher(Base):
    __tablename__ = "teachers"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="teacher")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LoginSession(Base):
    __tablename__ = "teacher_sessions"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    teacher_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("teachers.id", ondelete="CASCADE"), index=True)
    session_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginRateLimit(Base):
    __tablename__ = "login_rate_limits"
    ip_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_times: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Student(Base):
    __tablename__ = "students"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), index=True)
    roll_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    consent_given_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_recorded_by: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("teachers.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class SchoolClass(Base):
    __tablename__ = "classes"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    teacher_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("teachers.id"), index=True)
    room: Mapped[str | None] = mapped_column(String(80), nullable=True)
    schedule_days: Mapped[str] = mapped_column(String(40), default="")
    schedule_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    schedule_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    academic_term: Mapped[str] = mapped_column(String(80), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ClassEnrollment(Base):
    __tablename__ = "class_enrollments"
    __table_args__ = (UniqueConstraint("student_id", "class_id", name="uq_student_class"),)
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    student_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("students.id", ondelete="CASCADE"), index=True)
    class_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("classes.id", ondelete="CASCADE"), index=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClassSession(Base):
    __tablename__ = "class_sessions"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    class_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("classes.id", ondelete="CASCADE"), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AttendanceEvent(Base):
    __tablename__ = "attendance_events"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    student_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("students.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    camera_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="door")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    __table_args__ = (UniqueConstraint("student_id", "session_id", name="uq_attendance_student_session"),)
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    student_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("students.id"), index=True)
    session_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("class_sessions.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="absent")
    first_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    minutes_present: Mapped[int] = mapped_column(Integer, default=0)
    overridden_by: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("teachers.id"), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class FaceEmbedding(Base):
    __tablename__ = "face_embeddings"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    student_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("students.id", ondelete="CASCADE"), index=True)
    enrollment_session_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True, index=True)
    pose: Mapped[str] = mapped_column(String(24))
    encrypted_vector: Mapped[bytes] = mapped_column(LargeBinary)
    model_version: Mapped[str] = mapped_column(String(32), default="sface_2021dec")
    quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    yaw: Mapped[float | None] = mapped_column(Float, nullable=True)
    pitch: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EnrollmentSession(Base):
    __tablename__ = "enrollment_sessions"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    teacher_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("teachers.id"), index=True)
    student_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("students.id"), index=True)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    actor_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("teachers.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
