"""Reset an administrator password using a hidden CLI prompt."""

import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import models
from app.database import SessionLocal, engine, verify_database_connection, verify_migrations
from app.security import hash_password, password_is_weak


def main():
    verify_database_connection(engine)
    verify_migrations(engine)
    email = input("Administrator email: ").strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        print("Enter a valid email address.", file=sys.stderr)
        return 1
    password = getpass.getpass("New password (at least 10 characters): ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if password_is_weak(password):
        print("Choose a less common password with at least 10 characters.", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        admin = db.query(models.Teacher).filter_by(email=email, role="admin").one_or_none()
        if admin is None:
            print("No administrator exists with that email.", file=sys.stderr)
            return 1
        admin.password_hash = hash_password(password)
        admin.failed_attempts = 0
        admin.locked_until = None
        admin.session_version += 1
        db.commit()
    print("Administrator password updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
