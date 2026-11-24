# Face Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Native execution, no commits (folder is not a git repo and the owner has not asked for one).

**Goal:** A local web app that logs in only registered people, by face, with admin and normal roles.

**Architecture:** The browser streams camera JPEGs to Flask. The server reveals one head-turn at a time, checks liveness and identity itself, then issues a session. SQLite data lives in `%LOCALAPPDATA%\FaceLogin`, outside the project, so it can never be zipped.

**Tech Stack:** Python 3.13, Flask 3.1.3, opencv-python-headless 5.1.0.95 (YuNet, SFace), numpy, pytest, Windows DPAPI via ctypes.

**Spec:** `docs/superpowers/specs/2026-11-23-face-login-design.md`

## Global Constraints
- Bind 127.0.0.1 only; reject other Host headers; every POST needs a same-origin Origin header (CSRF).
- YuNet SHA-256 `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`; SFace `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79`.
- Session 15 min idle, 8 h max; step-up once, 2 min; 5 failures pause 5 min.
- No frames or photos written anywhere. Login failures show only "Not recognized."
- Starting thresholds (cosine, need calibration): user 0.50, admin 0.55, margin 0.08, duplicate 0.45, same person 0.50 facing / 0.30 turned. Yaw: facing < 0.12, turned > 0.28.
- A challenge is 4 random left/right turns, returning to centre after each; 45 s (enroll 120 s).

## Review Focus
- Abandoning a challenge after seeing the prompt, to dodge lockout: counts as a failure (Task 2).
- Garbage or huge image bytes: treated as no face, no crash (Task 1).
- Someone else stepping in mid-challenge: refused (Task 1).
- Admin demoted or removed while logged in: loses access on the next request (Task 2).
- Another user's face used for an admin's step-up: refused (Task 2).

### Task 1: `face.py` and `test_face.py`
**Produces:** `Face(vec, yaw)`; `analyze(jpeg: bytes) -> list[Face]`; `detect(img) -> list[Face]`; `identify(vec, users) -> user | None` (users have `.vec`, `.role`); `is_duplicate(vec, users) -> bool`; `Challenge(ttl)` with `.feed(faces) -> 'continue' | 'ok' | 'fail'`, `.prompt`, `.anchored`, `.vec`, `.reason`.
- [x] Spike: OpenCV 5.1 loads both models
- [x] Tests: unknown, ambiguous, admin stricter, correct turns pass, wrong turn, two faces, person swap, face lost, expired, garbage image is `[]`
- [x] Run (fail), implement, run (pass): `pytest test_face.py`

### Task 2: `store.py`, `app.py`, `test_app.py`
**Consumes:** Task 1. **Produces:** `store.users/add_user/remove_user/set_role/log/locked/new_session/get_session`; routes `/`, `/login`, `/logout`, `/api/challenge`, `/api/frame/<id>`, `/admin`, `/admin/enroll`, `/admin/role`, `/admin/remove`; CLI `run | enroll NAME [--admin] | remove NAME | list`.
- [x] Tests: login happy path, unknown, reused challenge, lockout, abandoned challenge counts, removed person, idle and absolute expiry, normal user refused on every admin route, step-up required/once/2 min/same person, last admin protected, demoted admin, duplicate face and name, wrong Origin and Host, templates not plaintext on disk
- [x] Run (fail), implement, run (pass): `pytest`

### Task 3: pages, packaging, handover
**Files:** `templates/*.html`, `static/camera.js`, `static/style.css`, `make_zip.py`, `test_zip.py`, `README.md`, `run.bat`, `requirements.txt`
- [x] Test: zip holds exactly the listed files and no database, log or `.venv`
- [x] Implement; check the real models through the browser pages with a stand-in video
- [ ] Owner, real camera: logs in; own photo and a different person refused; calibrate
