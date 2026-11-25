"""Database query boundary used by API routes and higher-level services."""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from . import models


def visible_classes(db: Session, user: models.Teacher):
    query = db.query(models.SchoolClass).filter(models.SchoolClass.active.is_(True))
    if user.role != "admin":
        query = query.filter(models.SchoolClass.teacher_id == user.id)
    return query.order_by(models.SchoolClass.name).all()


def get_class(db: Session, user: models.Teacher, class_id: str):
    item = db.get(models.SchoolClass, class_id)
    if item is None or (user.role != "admin" and item.teacher_id != user.id):
        raise HTTPException(status_code=404, detail="Class not found.")
    return item


def visible_student_ids(db: Session, user: models.Teacher) -> set[str] | None:
    if user.role == "admin":
        return None
    return {
        row[0] for row in db.query(models.ClassEnrollment.student_id)
        .join(models.SchoolClass, models.ClassEnrollment.class_id == models.SchoolClass.id)
        .filter(models.SchoolClass.teacher_id == user.id)
        .distinct()
    }


def get_student(db: Session, user: models.Teacher, student_id: str):
    student = db.get(models.Student, student_id)
    allowed = visible_student_ids(db, user)
    if student is None or (allowed is not None and student.id not in allowed):
        raise HTTPException(status_code=404, detail="Student not found.")
    return student


def attendance_percent(
    records: list[models.AttendanceRecord],
    late_counts_as_attended: bool = True,
) -> float | None:
    eligible = [record for record in records if record.status != "excused"]
    if not eligible:
        return None
    attended = sum(
        record.status == "present" or (late_counts_as_attended and record.status == "late")
        for record in eligible
    )
    return round(100.0 * attended / len(eligible), 2)


def calculate_session_status(
    first_in: datetime | None,
    session_date: date,
    start_time: time,
    end_time: time,
    grace_minutes: int,
) -> str:
    if first_in is None:
        return "absent"
    start = datetime.combine(session_date, start_time, tzinfo=first_in.tzinfo)
    end = datetime.combine(session_date, end_time, tzinfo=first_in.tzinfo)
    if first_in <= start + timedelta(minutes=grace_minutes):
        return "present"
    if first_in < end:
        return "late"
    return "absent"


def ensure_attendance_records(db: Session, session_row: models.ClassSession, grace_minutes: int):
    """Rebuild derived records from events; repeated calls produce the same record state."""
    enrollments = db.query(models.ClassEnrollment).filter_by(class_id=session_row.class_id).all()
    start = datetime.combine(session_row.session_date, session_row.start_time, tzinfo=timezone.utc)
    end = datetime.combine(session_row.session_date, session_row.end_time, tzinfo=timezone.utc)
    for enrollment in enrollments:
        record = db.query(models.AttendanceRecord).filter_by(
            student_id=enrollment.student_id, session_id=session_row.id
        ).first()
        if record is None:
            record = models.AttendanceRecord(
                student_id=enrollment.student_id,
                session_id=session_row.id,
                status="absent",
            )
            db.add(record)
            db.flush()
        if record.overridden_by is not None or session_row.status != "held":
            continue
        events = db.query(models.AttendanceEvent).filter(
            models.AttendanceEvent.student_id == enrollment.student_id,
            models.AttendanceEvent.timestamp >= start,
            models.AttendanceEvent.timestamp <= end,
        ).order_by(models.AttendanceEvent.timestamp).all()
        in_events = [event for event in events if event.direction == "in"]
        out_events = [event for event in events if event.direction == "out"]
        record.first_in = in_events[0].timestamp if in_events else None
        record.last_out = out_events[-1].timestamp if out_events else None
        record.status = calculate_session_status(
            record.first_in, session_row.session_date, session_row.start_time,
            session_row.end_time, grace_minutes,
        )
        if record.first_in and record.last_out and record.last_out > record.first_in:
            record.minutes_present = int((record.last_out - record.first_in).total_seconds() // 60)
        else:
            record.minutes_present = 0
    db.flush()


def student_records(db: Session, student_id: str, teacher_id: str | None = None):
    query = db.query(models.AttendanceRecord).join(
        models.ClassSession, models.AttendanceRecord.session_id == models.ClassSession.id
    ).join(models.ClassEnrollment, and_(
        models.ClassEnrollment.student_id == models.AttendanceRecord.student_id,
        models.ClassEnrollment.class_id == models.ClassSession.class_id,
    )).filter(
        models.AttendanceRecord.student_id == student_id,
        models.ClassSession.status == "held",
    )
    if teacher_id is not None:
        query = query.join(
            models.SchoolClass, models.ClassSession.class_id == models.SchoolClass.id
        ).filter(models.SchoolClass.teacher_id == teacher_id)
    return query.all()
