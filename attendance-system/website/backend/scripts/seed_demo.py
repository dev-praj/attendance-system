"""Create explicitly marked demo classes, students, sessions, events, and records."""

import argparse
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Base, SessionLocal, engine
from app.models import (
    AttendanceEvent, AttendanceRecord, ClassEnrollment, ClassSession, SchoolClass, Student, Teacher,
)


def clear_demo(db):
    session_ids = [row[0] for row in db.query(ClassSession.id).filter_by(is_demo=True).all()]
    student_ids = [row[0] for row in db.query(Student.id).filter_by(is_demo=True).all()]
    if session_ids:
        db.query(AttendanceRecord).filter(AttendanceRecord.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
    if student_ids:
        db.query(AttendanceEvent).filter(AttendanceEvent.student_id.in_(student_ids)).delete(
            synchronize_session=False
        )
        db.query(AttendanceRecord).filter(AttendanceRecord.student_id.in_(student_ids)).delete(
            synchronize_session=False
        )
        db.query(ClassEnrollment).filter(ClassEnrollment.student_id.in_(student_ids)).delete(
            synchronize_session=False
        )
    if session_ids:
        db.query(ClassSession).filter(ClassSession.id.in_(session_ids)).delete(synchronize_session=False)
    class_ids = [row[0] for row in db.query(SchoolClass.id).filter_by(is_demo=True).all()]
    if class_ids:
        db.query(ClassEnrollment).filter(ClassEnrollment.class_id.in_(class_ids)).delete(
            synchronize_session=False
        )
        db.query(ClassSession).filter(ClassSession.class_id.in_(class_ids)).delete(synchronize_session=False)
    db.query(Student).filter_by(is_demo=True).delete(synchronize_session=False)
    db.query(SchoolClass).filter_by(is_demo=True).delete(synchronize_session=False)
    db.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Replace existing marked demo data without prompting")
    args = parser.parse_args()
    if not args.yes and input("Create/replace clearly marked demo records? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("Cancelled.")
        return 0
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        admin = db.query(Teacher).filter_by(role="admin", active=True).first()
        if admin is None:
            print("Create an administrator first: python scripts/create_admin.py")
            return 1
        clear_demo(db)
        class_names = ("Biology 101", "Algebra 2", "World History")
        classes = []
        for index, name in enumerate(class_names):
            item = SchoolClass(
                name=name, code=f"DEMO-{index + 1}", teacher_id=admin.id, room=f"Room {index + 1}",
                schedule_days="Mon", schedule_start=time(9 + index, 0), schedule_end=time(10 + index, 0),
                academic_term="Demo term", is_demo=True,
            )
            db.add(item)
            classes.append(item)
        db.flush()
        students = []
        for index in range(30):
            student = Student(
                name=f"Demo Student {index + 1:02d}", roll_no=f"DEMO-{index + 1:03d}",
                email=f"demo{index + 1:02d}@example.com", active=True, is_demo=True,
            )
            db.add(student)
            students.append(student)
        db.flush()
        for index, student in enumerate(students):
            db.add(ClassEnrollment(student_id=student.id, class_id=classes[index // 10].id))
        today = date.today()
        attendance_rates = (0.95, 0.78, 0.48)
        for week in range(8):
            for class_index, item in enumerate(classes):
                session_date = today - timedelta(days=7 * (7 - week) + class_index)
                session = ClassSession(
                    class_id=item.id, session_date=session_date,
                    start_time=item.schedule_start, end_time=item.schedule_end, status="held", is_demo=True,
                )
                db.add(session)
                db.flush()
                for student_index in range(class_index * 10, (class_index + 1) * 10):
                    student = students[student_index]
                    rate = attendance_rates[student_index % 3]
                    present = ((week * 7 + student_index * 3) % 100) < rate * 100
                    late = present and (week + student_index) % 6 == 0
                    record = AttendanceRecord(
                        student_id=student.id, session_id=session.id,
                        status="late" if late else ("present" if present else "absent"),
                        minutes_present=48 if present else 0,
                    )
                    db.add(record)
                    if present:
                        first = datetime.combine(
                            session_date, time(9 + class_index, 7 if late else 0), tzinfo=timezone.utc
                        )
                        last = first + timedelta(minutes=48)
                        db.add(AttendanceEvent(
                            student_id=student.id, direction="in", timestamp=first,
                            score=0.82 if not student_index % 3 else 0.68, camera_id="demo-entry",
                            source="door", is_demo=True,
                        ))
                        db.add(AttendanceEvent(
                            student_id=student.id, direction="out", timestamp=last,
                            score=0.84, camera_id="demo-exit", source="door", is_demo=True,
                        ))
        db.commit()
    print("Demo data created: 3 classes, 30 marked demo students, 8 weeks of sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
