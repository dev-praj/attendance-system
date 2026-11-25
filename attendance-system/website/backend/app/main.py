"""FastAPI application, security middleware, startup schema, and production SPA hosting."""

import hmac
import json
import logging
import contextvars
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles
from fastapi import HTTPException

from . import models, security
from .api import router
from .database import SessionLocal, engine, verify_database_connection, verify_migrations
from .face.store import face_store
from .face.matching import DEFAULT_THRESHOLD
from .settings import WEBSITE_DIR, settings


request_id_context = contextvars.ContextVar("request_id", default="-")


class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_context.get(),
        }, ensure_ascii=True)


handler = logging.StreamHandler()
handler.setFormatter(JsonLogFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
logger = logging.getLogger(__name__)


def _verify_encryption_key(db):
    if not settings.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY is required to start the application.")
    try:
        cipher = Fernet(settings.encryption_key.encode("ascii"))
        face_store.verify()
    except (InvalidToken, ValueError, UnicodeEncodeError) as error:
        raise RuntimeError(
            "ENCRYPTION_KEY cannot decrypt the face vault. Restore its matching key and file from secure backup."
        ) from error
    except RuntimeError as error:
        raise RuntimeError(
            "The encrypted face vault is invalid or cannot be decrypted. Restore its matching ENCRYPTION_KEY."
        ) from error

    check = db.get(models.AppSetting, "encryption_key_check")
    if check is None:
        db.add(models.AppSetting(
            key="encryption_key_check",
            value=cipher.encrypt(b"attendance-encryption-key-check-v1").decode("ascii"),
        ))
        db.commit()
        return
    try:
        valid = hmac.compare_digest(
            cipher.decrypt(check.value.encode("ascii")),
            b"attendance-encryption-key-check-v1",
        )
    except (InvalidToken, ValueError, UnicodeEncodeError) as error:
        raise RuntimeError(
            "ENCRYPTION_KEY does not match the key previously used by this database. Restore the original key."
        ) from error
    if not valid:
        raise RuntimeError("ENCRYPTION_KEY check failed. Restore the original key from your secure backup.")


class SecurityHeadersAndCsrf(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/api/auth/login" and request.method == "POST":
            cookie = request.cookies.get("csrf_token", "")
            header = request.headers.get("x-csrf-token", "")
            if not cookie or not header or not hmac.compare_digest(cookie, header):
                response = JSONResponse(status_code=403, content={"detail": "CSRF validation failed."})
                self._headers(response)
                return response
        response = await call_next(request)
        self._headers(response)
        return response

    @staticmethod
    def _headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' http://localhost:8000 ws://localhost:5173; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=()")


def create_app(database_engine=None):
    database_engine = database_engine or engine

    @asynccontextmanager
    async def lifespan(application):
        settings.validate_runtime()
        verify_database_connection(database_engine)
        if settings.environment != "test":
            verify_migrations(database_engine)
        with SessionLocal(bind=database_engine) as db:
            _verify_encryption_key(db)
            for key, value in {
                "grace_period_minutes": "10",
                "late_counts_as_attended": "true",
                "low_attendance_threshold": "75",
                "face_match_threshold": str(DEFAULT_THRESHOLD),
                "academic_term": "",
                "data_retention_days": "365",
                "store_profile_photo": "false",
            }.items():
                if db.get(models.AppSetting, key) is None:
                    db.add(models.AppSetting(key=key, value=value))
            db.commit()
        yield

    application = FastAPI(
        title="Attendance System",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(SecurityHeadersAndCsrf)
    @application.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = str(uuid.uuid4())
        token = request_id_context.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            logger.exception("Unhandled request error.")
            raise
        finally:
            request_id_context.reset(token)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.app_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Device-Key"],
    )
    application.include_router(router)

    @application.exception_handler(404)
    async def not_found(request: Request, exc):
        # Production SPA routes are handled by the fallback below.
        return JSONResponse(status_code=404, content={"detail": "Not found."})

    frontend_dist = WEBSITE_DIR / "frontend" / "dist"
    assets = frontend_dist / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    async def frontend_fallback(full_path: str, request: Request):
        index_file = frontend_dist / "index.html"
        if index_file.is_file():
            if full_path == "login":
                return FileResponse(index_file)
            token = None
            try:
                token = request.cookies.get("attendance_session")
                if token:
                    claims = security.decode_session(token)
                    with SessionLocal(bind=database_engine) as db:
                        user = db.get(models.Teacher, claims["sub"])
                        login_session = db.get(models.LoginSession, claims["jti"])
                        expires_at = login_session.expires_at if login_session else None
                        if expires_at is not None and expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=timezone.utc)
                        if (
                            user is not None
                            and user.active
                            and user.session_version == claims.get("ver")
                            and login_session is not None
                            and login_session.teacher_id == user.id
                            and login_session.session_version == user.session_version
                            and login_session.revoked_at is None
                            and expires_at is not None
                            and expires_at > datetime.now(timezone.utc)
                        ):
                            return FileResponse(index_file)
            except HTTPException:
                pass
            return RedirectResponse("/login")
        if full_path in {"", "login"}:
            return JSONResponse(
                status_code=503,
                content={"detail": "Frontend is not built. Run python run.py dev or python run.py prod."},
            )
        return RedirectResponse("/login")

    return application


app = create_app()
