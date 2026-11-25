"""Password hashing, signed cookie sessions, CSRF, and shared authorization helpers."""

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .settings import settings


password_hasher = PasswordHasher()
_dummy_password_hash = password_hasher.hash(secrets.token_urlsafe(32))
_serializer = None


def _session_serializer():
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(settings.session_secret(), salt="attendance-session-v1")
    return _serializer


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return password_hasher.verify(encoded, password)
    except (VerifyMismatchError, ValueError):
        return False


def password_is_weak(password: str) -> bool:
    lowered = password.casefold()
    common = {"password123", "qwerty12345", "letmein1234", "welcome1234", "1234567890"}
    return len(password) < 10 or lowered in common or len(set(password)) < 4


def issue_session(user: models.Teacher, session_id: str) -> str:
    now = datetime.now(timezone.utc)
    return _session_serializer().dumps(
        {"sub": user.id, "ver": user.session_version, "jti": session_id, "iat": int(now.timestamp())}
    )


def decode_session(token: str) -> dict:
    try:
        payload = _session_serializer().loads(token, max_age=8 * 60 * 60)
    except BadSignature as error:
        raise HTTPException(status_code=401, detail="Please log in.") from error
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("sub"), str)
        or not isinstance(payload.get("jti"), str)
    ):
        raise HTTPException(status_code=401, detail="Please log in.")
    return payload


def audit(
    db: Session,
    action: str,
    *,
    actor_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: str = "",
    ip_address: str | None = None,
) -> None:
    db.add(models.AuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=detail[:2000],
        ip_address=ip_address,
    ))


def record_login_ip(ip_address: str, db: Session) -> None:
    key = hmac.new(
        settings.secret_key.encode("utf-8"),
        ip_address.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    now = datetime.now(timezone.utc)
    window = db.query(models.LoginRateLimit).filter_by(ip_hash=key).with_for_update().one_or_none()
    if window is None:
        window = models.LoginRateLimit(ip_hash=key)
        db.add(window)
        db.flush()
    attempts = [
        stamp for stamp in json.loads(window.attempt_times)
        if now.timestamp() - float(stamp) < 15 * 60
    ]
    if len(attempts) >= 20:
        window.attempt_times = json.dumps(attempts)
        window.updated_at = now
        db.commit()
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    attempts.append(now.timestamp())
    window.attempt_times = json.dumps(attempts)
    window.updated_at = now
    db.commit()


def check_csrf(request: Request) -> None:
    cookie = request.cookies.get("csrf_token")
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")


def current_user(request: Request, db: Session = Depends(get_db)) -> models.Teacher:
    token = request.cookies.get("attendance_session")
    if not token:
        raise HTTPException(status_code=401, detail="Please log in.")
    payload = decode_session(token)
    user = db.get(models.Teacher, payload["sub"])
    login_session = db.get(models.LoginSession, payload["jti"])
    expires_at = login_session.expires_at if login_session else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        user is None
        or not user.active
        or int(payload.get("ver", -1)) != user.session_version
        or login_session is None
        or login_session.teacher_id != user.id
        or login_session.session_version != user.session_version
        or login_session.revoked_at is not None
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=401, detail="Please log in.")
    return user


def require_admin(user: models.Teacher = Depends(current_user)) -> models.Teacher:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


def device_key_hash(key: str) -> str:
    if not settings.device_api_key_pepper:
        raise HTTPException(status_code=503, detail="Device authentication is not configured.")
    return hmac.new(
        settings.device_api_key_pepper.encode("utf-8"),
        key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def require_device(request: Request, db: Session = Depends(get_db)) -> models.Device:
    key = request.headers.get("x-device-key", "")
    if not key:
        raise HTTPException(status_code=401, detail="Invalid device credentials.")
    digest = device_key_hash(key)
    device = db.query(models.Device).filter_by(api_key_hash=digest, active=True).first()
    if device is None:
        raise HTTPException(status_code=401, detail="Invalid device credentials.")
    if settings.environment == "production" and request.url.scheme != "https":
        raise HTTPException(status_code=400, detail="Device API requires HTTPS in production.")
    return device
