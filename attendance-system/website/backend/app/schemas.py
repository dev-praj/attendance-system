"""Validated API input/output shapes. No schema returns biometric vectors."""

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class TeacherCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    role: Literal["teacher", "admin"] = "teacher"


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class StudentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    roll_no: str = Field(min_length=1, max_length=64)
    email: EmailStr | None = None
    class_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("roll_no")
    @classmethod
    def normalize_roll_no(cls, value):
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("SAN/USN is required.")
        return normalized


class StudentSubjectAssignmentInput(BaseModel):
    san: str = Field(min_length=1, max_length=64)
    class_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("san")
    @classmethod
    def normalize_san(cls, value):
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("SAN/USN is required.")
        return normalized


class ClassInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=32)
    teacher_id: str | None = None
    room: str | None = Field(default=None, max_length=80)
    schedule_days: str = Field(default="", max_length=40)
    schedule_start: time | None = None
    schedule_end: time | None = None
    academic_term: str = Field(default="", max_length=80)


class SessionInput(BaseModel):
    session_date: date
    start_time: time
    end_time: time
    status: Literal["scheduled", "held", "cancelled"] = "scheduled"
    repeat_weeks: int = Field(default=1, ge=1, le=52)


class SessionStatusInput(BaseModel):
    status: Literal["scheduled", "held", "cancelled"]


class BulkEnrollInput(BaseModel):
    roll_numbers: list[str] = Field(min_length=1, max_length=500)

    @field_validator("roll_numbers")
    @classmethod
    def normalize_roll_numbers(cls, values):
        normalized = [value.strip().upper() for value in values]
        if any(not value for value in normalized):
            raise ValueError("SAN/USN values cannot be blank.")
        return normalized


class PasswordReset(BaseModel):
    password: str = Field(min_length=10, max_length=256)


class OverrideInput(BaseModel):
    status: Literal["present", "late", "absent", "excused"]
    reason: str = Field(min_length=3, max_length=1000)


class ConsentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    roll_no: str = Field(min_length=1, max_length=64)
    email: EmailStr | None = None
    consent: bool
    consent_note: str | None = Field(default=None, max_length=1000)

    @field_validator("roll_no")
    @classmethod
    def normalize_roll_no(cls, value):
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("SAN/USN is required.")
        return normalized


class EnrollmentFrameInput(BaseModel):
    frame: str = Field(min_length=32, max_length=2_000_000)


class FaceVerificationInput(BaseModel):
    frames: list[str] = Field(min_length=8, max_length=8)
    direction: Literal["in", "out"] = "in"
    camera_id: str | None = Field(default=None, max_length=100)

    @field_validator("frames")
    @classmethod
    def validate_frame_sizes(cls, frames):
        if any(len(frame) > 500_000 for frame in frames):
            raise ValueError("Each camera frame must be no larger than 500 KB.")
        return frames


class DeviceEventInput(BaseModel):
    student_id: str | None = None
    student_roll_no: str | None = Field(default=None, max_length=64)
    direction: Literal["in", "out"]
    timestamp: datetime
    score: float | None = Field(default=None, ge=0, le=1)
    camera_id: str | None = Field(default=None, max_length=100)

    @field_validator("student_roll_no")
    @classmethod
    def normalize_student_roll_no(cls, value):
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("SAN/USN is required.")
        return normalized


class SettingsInput(BaseModel):
    grace_period_minutes: int = Field(ge=0, le=180)
    late_counts_as_attended: bool
    low_attendance_threshold: float = Field(ge=0, le=100)
    face_match_threshold: float = Field(ge=0.0, le=1.0)
    academic_term: str = Field(max_length=80)
    data_retention_days: int = Field(ge=1, le=3650)
    store_profile_photo: bool = False


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TeacherOutput(ORMModel):
    id: str
    name: str
    email: EmailStr
    role: str
    active: bool


class StudentOutput(ORMModel):
    id: str
    name: str
    roll_no: str
    email: EmailStr | None
    active: bool
