"""Create the first administrator interactively; no default credentials are created."""

import getpass
import re
import sys
from pathlib import Path

from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal, engine, verify_database_connection, verify_migrations
from app.models import Teacher
from app.security import hash_password, password_is_weak


def main():
    verify_database_connection(engine)
    verify_migrations(engine)
    with SessionLocal() as db:
        if db.query(Teacher).filter_by(role="admin", active=True).first():
            print("An active administrator already exists.")
            return 1
        name = input("Administrator name: ").strip()
        email = input("Administrator email: ").strip().casefold()
        if not name or len(name) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            print("Enter a valid name and email address.", file=sys.stderr)
            return 1
        password = getpass.getpass("New password (at least 10 characters): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1
        if password_is_weak(password):
            print("Choose a less common password with at least 10 characters.", file=sys.stderr)
            return 1
        db.add(Teacher(name=name, email=email, password_hash=hash_password(password), role="admin"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            print("That email is already in use.", file=sys.stderr)
            return 1
    print("Administrator created. Keep the password private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
