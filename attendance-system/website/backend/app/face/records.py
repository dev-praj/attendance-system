"""Metadata wrapper for encrypted face vectors held outside the application database."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class FaceVectorRecord:
    id: str
    student_id: str
    enrollment_session_id: str | None
    pose: str
    encrypted_vector: bytes
    model_version: str
    quality: float | None
    yaw: float | None
    pitch: float | None
    created_at: datetime
