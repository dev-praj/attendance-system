# Project Overview

This repository contains a teacher-focused attendance system that uses face recognition to record student check-ins and check-outs. It has two related Python face-recognition projects:

- **Full attendance website:** The main application is under `attendance-system/website`. It provides a React and TypeScript teacher dashboard backed by a FastAPI API, SQLAlchemy, Alembic migrations, and Supabase PostgreSQL.
- **Standalone face scanner:** The `face_test` project is a small CPU-only webcam tool for testing face enrollment, pose coverage, threshold calibration, and verification without a database or website.

## Main Website

The website is designed for authorized teachers rather than student logins. It supports classes and student enrollment, scheduled sessions, attendance records and overrides, CSV reports, configurable attendance rules, audit logging, registered-device access, and administrative teacher management.

The frontend provides dashboard, class, student, face registration, scan/verification, report, settings, teacher, device, and audit pages. Authentication uses Supabase with persistent server sessions, CSRF protection, rate limiting, security headers, and role-based access. Teacher and administrative APIs are managed in FastAPI.

Face enrollment uses the browser camera and the OpenCV YuNet and SFace models to detect faces, estimate head pose, collect multiple poses, and create 128-dimensional normalized face embeddings. Verification compares several camera frames against enrolled templates using cosine similarity and multi-frame voting. Camera frames are processed in memory and are not saved.

## Data and Security

Attendance, student, class, session, and consent metadata is stored in Supabase/PostgreSQL. Encrypted face embeddings and enrollment progress are kept in a separate server-side face vault, not in the database. An encryption key and the vault must be preserved together; losing either can make biometric data unusable. The backend applies migration checks, encrypted-key validation, and model checks during startup.

Face recognition is not liveness or anti-spoofing detection. It is a learning and classroom-management tool, and it should not be used as the sole security or access-control mechanism.

## Standalone Face Test

The `face_test` directory contains a local OpenCV-based command-line demonstration. It supports nine-pose enrollment, pose and embedding quality checks, threshold calibration, eight-frame verification voting, and enrollment listing/deletion. It stores normalized embeddings and pose metadata locally; it does not store camera images or video and has no database or liveness detection.

The standalone tool uses the same family of YuNet/SFace models and can be used to understand or tune face matching before using the full website. It is not a substitute for the attendance system's consent, audit, reporting, and database features.

## Technology Summary

- **Backend:** Python, FastAPI, SQLAlchemy, Alembic, Pydantic
- **Frontend:** React, TypeScript, Vite, React Query, React Router
- **Database:** Supabase PostgreSQL; SQLite is limited to isolated tests
- **Face recognition:** OpenCV, YuNet face detection, SFace recognition, CPU processing
- **Security:** Supabase Auth, encrypted face vault, persistent sessions, CSRF and rate-limit protections
- **Testing:** Pytest for backend tests, unittest for the standalone tool, and frontend build/lint checks
