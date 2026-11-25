"""Temporary, opt-in face-enrollment timing diagnostics; remove after measurement."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar


logger = logging.getLogger("app.face.enrollment_timing")
POSE_CODES = {
    "CENTER": 1, "LEFT": 2, "RIGHT": 3, "UP": 4, "DOWN": 5,
    "UP-LEFT": 6, "UP-RIGHT": 7, "DOWN-LEFT": 8, "DOWN-RIGHT": 9,
}
REJECTION_CODES = {
    "session_missing": 1, "session_expired": 2, "invalid_frame": 3,
    "no_face": 4, "multiple_faces": 5, "low_score": 6,
    "too_small": 7, "too_large": 8, "off_center": 9, "blurry": 10,
    "moving": 11, "invalid_pose": 12, "duplicate": 13,
    "stable_wait": 14, "self_test_not_centered": 15,
    "self_test_quality": 16, "self_test_similarity": 17,
    "attempt_timeout": 18, "duplicate_identity": 19,
}

_current: ContextVar[dict | None] = ContextVar("face_enrollment_timing", default=None)
_stats_lock = threading.Lock()
_sessions: dict[str, dict] = {}


def enabled() -> bool:
    """Diagnostics are available only in development and default to off."""
    return (
        os.getenv("APP_ENV", "development").strip().lower() == "development"
        and os.getenv("FACE_ENROLLMENT_TIMING", "").strip() == "1"
    )


def add_stage(name: str, milliseconds: float) -> None:
    profile = _current.get()
    if profile is not None:
        profile["stages"][name] = profile["stages"].get(name, 0.0) + float(milliseconds)


@contextmanager
def measure(name: str):
    if _current.get() is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        add_stage(name, (time.perf_counter() - started) * 1000.0)


def reject(code: str) -> None:
    profile = _current.get()
    if profile is None:
        return
    numeric_code = REJECTION_CODES[code]
    profile["rejection"] = numeric_code
    profile["pose"] = 0
    with _stats_lock:
        profile["rejections"][numeric_code] = profile["rejections"].get(numeric_code, 0) + 1


def accepted(pose: str) -> None:
    profile = _current.get()
    if profile is None:
        return
    numeric_pose = POSE_CODES[pose]
    profile["rejection"] = 0
    profile["pose"] = numeric_pose
    now = time.perf_counter()
    with _stats_lock:
        last = profile["session_stats"].get("last_accepted_at")
        profile["accepted_interval_ms"] = None if last is None else (now - last) * 1000.0
        profile["session_stats"]["last_accepted_at"] = now
        profile["session_stats"]["accepted"][numeric_pose] = (
            profile["session_stats"]["accepted"].get(numeric_pose, 0) + 1
        )


@contextmanager
def frame_profile(session_id: str, endpoint_code: int):
    if not enabled():
        yield
        return
    with _stats_lock:
        stats = _sessions.setdefault(
            session_id, {"frame": 0, "rejections": {}, "accepted": {}, "last_accepted_at": None}
        )
        stats["frame"] += 1
        frame_number = stats["frame"]
    profile = {
        "stages": {}, "rejection": 0, "pose": 0,
        "accepted_interval_ms": None, "rejections": stats["rejections"],
        "session_stats": stats,
    }
    token = _current.set(profile)
    started = time.perf_counter()
    try:
        yield profile
    finally:
        profile["stages"]["handler_total"] = (time.perf_counter() - started) * 1000.0
        with _stats_lock:
            rejection_counts = dict(profile["rejections"])
            accepted_counts = dict(profile["session_stats"]["accepted"])
        record = {
            "endpoint": int(endpoint_code),
            "frame": int(frame_number),
            "stages_ms": {key: round(value, 3) for key, value in profile["stages"].items()},
            "reject": int(profile["rejection"]),
            "pose": int(profile["pose"]),
            "accepted_interval_ms": (
                round(profile["accepted_interval_ms"], 3)
                if profile["accepted_interval_ms"] is not None else None
            ),
            "rejection_counts": {str(key): int(value) for key, value in rejection_counts.items()},
            "accepted_counts": {str(key): int(value) for key, value in accepted_counts.items()},
        }
        logger.info("face_enrollment_timing %s", json.dumps(record, separators=(",", ":")))
        _current.reset(token)
        if profile.get("terminal"):
            with _stats_lock:
                _sessions.pop(session_id, None)


def timed_frame_endpoint(endpoint_code: int):
    """Wrap a synchronous FastAPI frame handler without changing its signature."""
    from functools import wraps

    def decorate(handler):
        @wraps(handler)
        def wrapped(session_id, *args, **kwargs):
            with frame_profile(session_id, endpoint_code) as profile:
                result = handler(session_id, *args, **kwargs)
                if profile is not None and isinstance(result, dict) and result.get("status") in {"complete", "failed", "blocked"}:
                    profile["terminal"] = True
                return result
        return wrapped
    return decorate

