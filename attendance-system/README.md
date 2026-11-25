# Teacher attendance system

A teacher-only attendance website for Windows, macOS, and Linux. It uses FastAPI, SQLAlchemy, Supabase PostgreSQL, React, TypeScript, and OpenCV YuNet + SFace on CPU. There is no student-facing login.

## Install

Install Python 3.11+, Node.js 20+, and npm. A virtual environment is recommended on every OS.

From `attendance-system/website`:

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
Set-Location frontend
npm install
Set-Location ..
Copy-Item .env.example .env
```

### macOS/Linux terminal

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
cd frontend
npm install
cd ..
cp .env.example .env
```

Configure `DATABASE_URL` using the PostgreSQL URI from Supabase's **Connect** panel; prefer the Session pooler on port 5432. The Supabase project HTTPS URL is not a database URL. `sslmode=require` is enforced by the backend. Set `SUPABASE_URL` and `SUPABASE_ANON_KEY` for sign-in. From `website/backend`, run `python scripts/generate_secrets.py` to generate missing `SECRET_KEY`, `ENCRYPTION_KEY`, and `DEVICE_API_KEY_PEPPER` values directly into `website/.env`; it never prints secret values.

Use the same `SECRET_KEY` and `ENCRYPTION_KEY` on all hosts. Back up `ENCRYPTION_KEY` somewhere safe: losing it makes stored face data unreadable. Neither the Supabase anon/publishable key nor service-role/secret key belongs in frontend code. Do not expose a service-role key to the app.

Attendance, student/class records, and consent status are stored in Supabase. Camera frames are processed in memory and are not saved. Encrypted face templates and enrollment progress are stored in a separate server-side vault file at `FACE_STORE_PATH`, never in Supabase. This path must point to a durable, access-restricted volume in production, shared by all app workers. Back up the vault file together with its matching `ENCRYPTION_KEY`; without both, stored face templates cannot be recovered.

## Migrations and first admin

From `website/backend`:

```text
python scripts/download_models.py
python -m alembic upgrade head
python scripts/create_admin.py
```

The admin prompt creates an approved app profile; use the same email as the Supabase Auth account. The profile's local password hash is not used for sign-in when Supabase Auth is enabled. Subsequent teacher profiles are created by an admin in the Teachers page. Revision 0006 moves any existing encrypted database face templates and enrollment state into the vault before removing their database tables; verify that migration and back up the vault before deployment.

## Run locally

From `attendance-system/website`:

```text
python run.py dev
```

Open the frontend URL printed by the launcher (normally `http://localhost:5173`). Startup requires PostgreSQL, verifies migration state and encryption-key compatibility, and does not fall back to SQLite. Press Ctrl+C to stop the API and frontend.

## Existing SQLite data

SQLite is enabled only for isolated automated tests. If an earlier local database exists, back it up and run `python scripts/migrate_sqlite_to_supabase.py` from `website/backend`. The script copies application rows in dependency order, skips existing destination rows, and leaves the source SQLite file untouched. Legacy integer IDs are converted deterministically to UUIDs while foreign-key relationships are preserved, so existing ID values change. Biometric rows from old SQLite databases are intentionally skipped rather than copied to Supabase. Run `python -m alembic upgrade head` before copying.

## Tests

From `website/backend`:

```text
python -m pytest
```

From `website/frontend`:

```text
npm run build
npm run lint
```

The automated backend suite uses an isolated in-memory SQLite database and never targets Supabase.

## Production and pre-deploy checklist

Set `APP_ENV=production` and configure `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SECRET_KEY`, `ENCRYPTION_KEY`, `DEVICE_API_KEY_PEPPER`, `FACE_STORE_PATH`, `APP_ORIGIN`, and `PORT`. Mount durable storage at the configured `FACE_STORE_PATH` (for example, `/var/lib/attendance/face-vault.enc`) and ensure only the application user can access it. All workers must use that same persistent file volume. `python run.py prod` applies migrations, builds the frontend, and serves the API and frontend together. The Docker image downloads the official YuNet/SFace models and loads both during the image build, so a model download or compatibility failure stops the build rather than surfacing during a live scan.

Before deploying:

1. Run `python scripts/doctor.py` from `website/backend` and resolve every failure.
2. Keep the same `SECRET_KEY` and `ENCRYPTION_KEY` as the trusted local environment.
3. Back up `ENCRYPTION_KEY` securely.
4. Set `APP_ORIGIN` to the exact HTTPS website origin and enable HTTPS.
5. Confirm the Supabase project is active and not paused.

For a VPS or laptop server, use Caddy as an HTTPS reverse proxy to `127.0.0.1:8000`, and keep the app bound to localhost. For Render/Railway, deploy the included Dockerfile, bind to `0.0.0.0`, and use the platform's `PORT`. Add the environment variables above in the platform secret store; never copy `.env` into the container or commit it. Browser camera registration and verification require HTTPS (localhost is allowed by browsers).

## Face enrollment and verification

Sign in as a teacher, open **Register Face**, record consent, choose the student's class, and follow all nine pose prompts. Enrollment uses the browser camera and posts frames to the backend for YuNet/SFace processing; frames are transient, and encrypted embeddings plus in-progress enrollment state are stored only in the separate file vault, not in Supabase. The separate **Scan / verify** page captures eight frames and verifies them against completed, consented enrollments visible to the signed-in teacher. It accepts only when at least six frames pass the configured cosine threshold and, when there are multiple enrolled people, the 0.05 identity margin. Accepted scans record a check-in or check-out event; rejected or unclear scans do not create attendance events. Set the face threshold under **Settings**; it defaults to `0.50`.

The browser must grant camera permission, the camera must not be occupied by another application, and deployed sites must use HTTPS. The backend must have both model files; the Docker build verifies this automatically. Direct non-Docker deployments must run `python scripts/download_models.py` from `website/backend`. Face matching is not liveness detection or anti-spoofing; supervise scans and do not use this learning project as a sole security or access-control measure.

Example Caddy site:

```caddyfile
attendance.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Privacy and security

- Obtain and record student consent before face enrollment; withdraw consent when requested.
- Face vectors and enrollment state are encrypted in the separate file vault at `FACE_STORE_PATH`, not in Supabase; losing either that file or its matching `ENCRYPTION_KEY` makes face data irrecoverable.
- Face images/video frames are processed in memory and are not stored. Teacher APIs do not return vectors. The device-authenticated embeddings route reads from the encrypted file vault and intentionally returns decrypted templates to authorized door devices; protect device keys and use HTTPS.
- Keep `.env`, databases, model files, and backups out of source control. The app is a learning project, not a production security, biometric, or access-control certification.
