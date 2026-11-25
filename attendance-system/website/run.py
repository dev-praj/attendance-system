"""Cross-platform local launcher: python run.py dev | prod."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def resolve_python():
    candidates = [
        ROOT / ".venv",
        BACKEND / ".venv",
    ]
    for base in candidates:
        if os.name == "nt":
            candidate = base / "Scripts" / "python.exe"
        else:
            candidate = base / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return sys.executable


def find_free_port(start_port, attempts=20):
    for candidate in range(start_port, start_port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError(f"Could not find a free port starting at {start_port}.")


def run(command, cwd, env=None, wait=True):
    return subprocess.run(command, cwd=cwd, env=env, check=False) if wait else subprocess.Popen(command, cwd=cwd, env=env)


def validate_environment(env):
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url.casefold().startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        print("DATABASE_URL must be the PostgreSQL URI from Supabase Connect, not the project HTTPS URL.")
        return False
    required = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SECRET_KEY", "ENCRYPTION_KEY", "DEVICE_API_KEY_PEPPER")
    missing = [key for key in required if not env.get(key, "").strip()]
    if missing:
        print(f"Required environment variables are missing: {', '.join(missing)}.")
        return False
    return True


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"dev", "prod"}:
        print("Usage: python run.py dev|prod")
        return 2

    load_dotenv(ROOT / ".env", override=False)

    mode = sys.argv[1]
    env = os.environ.copy()
    host = env.get("HOST", "127.0.0.1")
    python = resolve_python()
    npm = "npm.cmd" if os.name == "nt" else "npm"

    if mode == "prod":
        env["APP_ENV"] = "production"
        if not validate_environment(env):
            return 1
        validation = run(
            [python, "-c", "from app.settings import settings; settings.validate_runtime()"],
            BACKEND, env=env,
        )
        if validation.returncode:
            return validation.returncode
        migration = run([python, "-m", "alembic", "upgrade", "head"], BACKEND, env=env)
        if migration.returncode:
            return migration.returncode
        result = run([npm, "run", "build"], FRONTEND, env=env)
        if result.returncode:
            return result.returncode
        backend_port = env.get("PORT", "8000")
        return run(
            [python, "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND),
             "--host", host, "--port", backend_port],
            BACKEND, env=env, wait=True,
        ).returncode

    backend_port = find_free_port(int(env.get("BACKEND_PORT", "8000")))
    frontend_port = find_free_port(int(env.get("FRONTEND_PORT", "5173")))
    env["APP_ORIGIN"] = f"http://{host}:{frontend_port}"
    env["PORT"] = str(backend_port)
    env["VITE_API_PROXY_TARGET"] = f"http://{host}:{backend_port}"

    if not validate_environment(env):
        return 1
    validation = run(
        [python, "-c", "from app.settings import settings; settings.validate_runtime()"],
        BACKEND, env=env,
    )
    if validation.returncode:
        return validation.returncode
    migration = run([python, "-m", "alembic", "upgrade", "head"], BACKEND, env=env)
    if migration.returncode:
        return migration.returncode

    processes = []
    try:
        backend = subprocess.Popen(
            [python, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(backend_port)],
            cwd=BACKEND,
            env=env,
        )
        processes.append(backend)
        frontend_env = env.copy()
        frontend = subprocess.Popen(
            [npm, "run", "dev", "--", "--host", host, "--port", str(frontend_port)],
            cwd=FRONTEND,
            env=frontend_env,
        )
        processes.append(frontend)
        print(f"Development servers started: frontend http://{host}:{frontend_port}, API http://{host}:{backend_port}")
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        return next((process.returncode for process in processes if process.returncode not in (0, None)), 0)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
