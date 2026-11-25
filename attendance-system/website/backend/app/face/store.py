"""Encrypted, file-backed biometric storage kept separate from application tables."""

import base64
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
from cryptography.fernet import Fernet, InvalidToken

from ..settings import BACKEND_DIR, settings
from . import timing


class FaceStore:
    def __init__(self, path: Path | None = None):
        configured_path = getattr(settings, "face_store_path", None)
        self.path = Path(path or configured_path or BACKEND_DIR / "data" / "face-vault.enc")
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _cipher():
        try:
            return Fernet(settings.encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise RuntimeError("ENCRYPTION_KEY must be configured to access face data.") from error

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                with timing.measure("vault_lock_wait"):
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with timing.measure("vault_lock_wait"):
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self):
        if not self.path.exists():
            return {"version": 1, "templates": {}, "sessions": {}}
        try:
            with timing.measure("vault_disk_read"):
                encrypted = self.path.read_bytes()
            with timing.measure("vault_decrypt_json"):
                plaintext = self._cipher().decrypt(encrypted)
                data = json.loads(plaintext)
        except (OSError, InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "The encrypted face store is unreadable. Restore its file and the matching ENCRYPTION_KEY."
            ) from error
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("templates"), dict)
            or not isinstance(data.get("sessions"), dict)
        ):
            raise RuntimeError("The encrypted face store has an unsupported or invalid format.")
        return data

    def _write_unlocked(self, data):
        with timing.measure("vault_encrypt_json"):
            encrypted = self._cipher().encrypt(
                json.dumps(data, separators=(",", ":"), allow_nan=False).encode("utf-8")
            )
        temporary_path = None
        try:
            with timing.measure("vault_temp_write_fsync"):
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=self.path.name + ".", suffix=".tmp",
                    dir=self.path.parent, delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(encrypted)
                    temporary.flush()
                    os.fsync(temporary.fileno())
            if os.name != "nt":
                temporary_path.chmod(0o600)
            with timing.measure("vault_atomic_replace"):
                os.replace(temporary_path, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @contextmanager
    def _edit(self):
        with timing.measure("vault_edit_total"):
            with self._lock():
                data = self._read_unlocked()
                yield data
                self._write_unlocked(data)

    def verify(self):
        with self._lock():
            data = self._read_unlocked()
            cipher = self._cipher()
            records = [
                item
                for templates in data["templates"].values()
                for item in templates
            ]
            records.extend(
                item
                for session in data["sessions"].values()
                for item in session["embeddings"]
            )
            for record in records:
                try:
                    vector = np.frombuffer(
                        cipher.decrypt(
                            base64.b64decode(record["encrypted_vector"], validate=True)
                        ),
                        dtype=np.float32,
                    )
                except (InvalidToken, ValueError) as error:
                    raise RuntimeError(
                        "A stored face template cannot be decrypted with ENCRYPTION_KEY."
                    ) from error
                if vector.shape != (128,) or not np.isfinite(vector).all() or np.linalg.norm(vector) <= 0:
                    raise RuntimeError("A stored face template is invalid.")
            self._write_unlocked(data)
        return True

    def create_session(self, session_id, teacher_id, student_id, state, expires_at):
        with self._edit() as data:
            data["sessions"][session_id] = {
                "teacher_id": teacher_id,
                "student_id": student_id,
                "state": state,
                "expires_at": expires_at.isoformat(),
                "completed": False,
                "embeddings": [],
            }

    def get_session(self, session_id):
        with timing.measure("vault_session_read"):
            with self._lock():
                session = self._read_unlocked()["sessions"].get(session_id)
        return FaceEnrollmentSession(self, session_id, session) if session is not None else None

    def update_session(self, session_id, changes):
        with timing.measure("vault_session_write"):
            with self._edit() as data:
                session = data["sessions"].get(session_id)
                if session is None:
                    raise RuntimeError("The face enrollment session no longer exists.")
                session.update(changes)

    def delete_session(self, session_id):
        with self._edit() as data:
            data["sessions"].pop(session_id, None)

    @staticmethod
    def _embedding_record(raw):
        from .records import FaceVectorRecord

        return FaceVectorRecord(
            id=raw["id"],
            student_id=raw["student_id"],
            enrollment_session_id=raw.get("enrollment_session_id"),
            pose=raw["pose"],
            encrypted_vector=base64.b64decode(raw["encrypted_vector"], validate=True),
            model_version=raw.get("model_version", "sface_2021dec"),
            quality=raw.get("quality"),
            yaw=raw.get("yaw"),
            pitch=raw.get("pitch"),
            created_at=datetime.fromisoformat(raw["created_at"]),
        )

    def _serialize_embedding(self, record, *, enrollment_session_id=None):
        encrypted = record.encrypted_vector
        if not isinstance(encrypted, bytes):
            encrypted = bytes(encrypted)
        return {
            "id": str(record.id),
            "student_id": str(record.student_id),
            "enrollment_session_id": enrollment_session_id,
            "pose": record.pose,
            "encrypted_vector": base64.b64encode(encrypted).decode("ascii"),
            "model_version": getattr(record, "model_version", "sface_2021dec"),
            "quality": record.quality,
            "yaw": record.yaw,
            "pitch": record.pitch,
            "created_at": record.created_at.isoformat(),
        }

    def add_session_embedding(self, session_id, record):
        with timing.measure("vault_session_write"):
            with self._edit() as data:
                session = data["sessions"].get(session_id)
                if session is None:
                    raise RuntimeError("The face enrollment session no longer exists.")
                session["embeddings"].append(
                    self._serialize_embedding(record, enrollment_session_id=session_id)
                )

    def list_session_embeddings(self, session_id, student_id=None, pose=None):
        with timing.measure("vault_embedding_read"):
            with self._lock():
                data = self._read_unlocked()
        session = data["sessions"].get(session_id)
        if session is None or (
            student_id is not None and session["student_id"] != student_id
        ):
            return []
        records = [self._embedding_record(item) for item in session["embeddings"]]
        return [item for item in records if pose is None or item.pose == pose]

    def delete_session_embeddings(self, session_id):
        with self._edit() as data:
            session = data["sessions"].get(session_id)
            if session is not None:
                session["embeddings"] = []

    def delete_session_embedding(self, session_id, embedding_id):
        with self._edit() as data:
            session = data["sessions"].get(session_id)
            if session is not None:
                session["embeddings"] = [
                    item for item in session["embeddings"] if item["id"] != embedding_id
                ]

    def list_templates(self, student_id=None):
        with self._lock():
            data = self._read_unlocked()
        selected = (
            {student_id: data["templates"].get(student_id, [])}
            if student_id is not None
            else data["templates"]
        )
        return [
            self._embedding_record(item)
            for records in selected.values()
            for item in records
        ]

    def has_templates(self, student_id):
        with self._lock():
            return bool(self._read_unlocked()["templates"].get(student_id))

    def replace_templates(self, student_id, records, session_id=None):
        with self._edit() as data:
            data["templates"][student_id] = [
                self._serialize_embedding(record) for record in records
            ]
            if session_id is not None:
                data["sessions"].pop(session_id, None)

    def delete_templates(self, student_id):
        with self._edit() as data:
            data["templates"].pop(student_id, None)
            data["sessions"] = {
                session_id: session
                for session_id, session in data["sessions"].items()
                if session["student_id"] != student_id
            }

    def import_legacy(self, embeddings, sessions):
        """Preserve old DB records in the encrypted vault before the removal migration."""
        with self._edit() as data:
            for row in sessions:
                if row.id in data["sessions"]:
                    continue
                data["sessions"][row.id] = {
                    "teacher_id": row.teacher_id,
                    "student_id": row.student_id,
                    "state": json.loads(row.state_json),
                    "expires_at": row.expires_at.isoformat(),
                    "completed": row.completed,
                    "embeddings": [],
                }
            for row in embeddings:
                record = self._serialize_embedding(
                    row, enrollment_session_id=row.enrollment_session_id
                )
                session_id = row.enrollment_session_id
                if session_id is None:
                    existing = {
                        item["id"] for item in data["templates"].get(row.student_id, [])
                    }
                    if record["id"] not in existing:
                        data["templates"].setdefault(row.student_id, []).append(record)
                else:
                    enrollment = data["sessions"].get(session_id)
                    if enrollment is None:
                        raise RuntimeError(
                            "An in-progress face template has no enrollment session; migration stopped."
                        )
                    existing = {item["id"] for item in enrollment["embeddings"]}
                    if record["id"] not in existing:
                        enrollment["embeddings"].append(record)


class FaceEnrollmentSession:
    def __init__(self, store, session_id, data):
        self._store = store
        self.id = session_id
        self.teacher_id = data["teacher_id"]
        self.student_id = data["student_id"]
        self.expires_at = datetime.fromisoformat(data["expires_at"])
        self._completed = data["completed"]
        self._state = data["state"]

    @property
    def state_json(self):
        with timing.measure("session_state_serialize"):
            return json.dumps(self._state)

    @state_json.setter
    def state_json(self, value):
        with timing.measure("session_state_deserialize"):
            self._state = json.loads(value)
        self._store.update_session(self.id, {"state": self._state})

    @property
    def completed(self):
        return self._completed

    @completed.setter
    def completed(self, value):
        self._completed = bool(value)
        self._store.update_session(self.id, {"completed": self._completed})


face_store = FaceStore()
