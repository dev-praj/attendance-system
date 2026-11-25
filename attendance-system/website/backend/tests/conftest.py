"""Use an isolated in-memory SQLite database only for automated tests."""

import os


os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
