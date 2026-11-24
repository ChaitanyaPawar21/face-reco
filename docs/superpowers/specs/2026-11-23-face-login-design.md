# Face login: short design

Date: 2026-11-23. Status: approved by the owner 2026-11-23.

**Partly superseded 2026-11-23:** the owner asked for photo upload plus username and password,
followed by the camera check. Both are required; a password alone never logs in. See README.md.

**2026-11-24:** the owner replaced photo upload with Face ID style camera registration (same head
turns as login; only the encrypted template is kept), and allowed one specific login message,
"More than one face in view". Considered and dropped: keeping upload as well (does not prove who
was there) and a single snapshot (a printed photo could pass).

**2026-11-24, hosting:** the owner asked for whatever is free and best. Builder's calls: Vercel pages,
Render free API, Neon free Postgres; one-use invite links instead of registering at the admin's PC;
delete-my-data; TEMPLATE_KEY instead of DPAPI when hosted. This ends "face data never leaves the PC"
for the hosted version only. Dropped: paid Render disk (costs money), Render free Postgres (expires
after 30 days), Hugging Face (Docker needs a paid plan), a tunnel to the PC (it must stay on).

## Intent
An app only registered, authorized people can log in to. The face is the only
credential: no recognized face, no login. The owner asked for "super secure"
and chose face only "right now", so a second factor may follow. The owner wants to
send it to their higher-up, with admin and normal user roles.

## Non-goals
Passkey, password or phone code; public sign-up; one shared server that several
computers log in to; production hosting.

## Anti-goals
- Any path to the protected page that skips the face check.
- A normal user reaching any admin page or action.
- Anyone's face data leaving the computer it was registered on.
- A password fallback.
- Guessing the closest person when the match is unclear. Unclear means denied.

## Approaches
- A. Face only: fastest, but a good replayed video or deepfake may pass.
  **Chosen by the owner.**
- B. Face + passkey: a fooled camera is not enough. Recommended, not chosen.
- C. Face + password + phone code: works anywhere, slower, phishable.

## Design (builder's calls unless noted)
- Platform: Python, Flask, SQLite, one local web app on 127.0.0.1 only.
- Recognition: OpenCV YuNet (detect) and SFace (match), on this machine.
  Model files are pinned by SHA-256 and refused if they differ.
- Roles (asked for by the owner; the details are builder's calls):
  - Normal user: log in, see the protected page, log out.
  - Admin: also register people, set roles, remove people, read the audit log.
  - The server checks the role from the database on every request.
  - Registering, role changes and removals need a fresh admin face check, valid
    once and for 2 minutes, and are audit logged.
  - Admins must pass a stricter match threshold.
  - The last admin cannot be removed or demoted.
- Register: first admin by a command on the PC, `python app.py enroll NAME
  --admin`. After that, admins register people from the admin page. There is no
  public sign-up. Several samples are taken; a face already registered under
  another name is refused. `remove` and `list` commands remain for recovery.
- Handover: a zip of code, models and a README, built from an explicit file list
  so the database, templates and logs are never included. The recipient runs it
  on their own PC and registers themselves as its first admin. Separate copies
  share no users.
- Login, with no username:
  1. Server issues a one-time random sequence of head turns, short expiry.
  2. Browser sends camera frames; the server checks the turns happened in order.
  3. Exactly one face per frame, the same person throughout.
  4. Must clear a strict threshold AND beat the second-best person by a margin.
  5. Success creates a fresh server-side session.
- Every request rechecks the session and that the person is still registered,
  so removal takes effect immediately.
- Sessions: random token stored hashed; HttpOnly, SameSite=Strict cookie; 15 min
  idle, 8 h maximum; CSRF protection; logout.
- Abuse limits: 5 failures pause login for 5 minutes, for everyone on this app.
  There is no username, so there is no single account to lock.
- Storage: templates encrypted with Windows DPAPI (tied to this Windows user);
  no photos or frames saved.
- Audit log: time, outcome, reason, matched name. The user sees only a generic
  "not recognized".
- After login: a simple protected page, where the real app plugs in later.

## Known limits
- An ordinary camera cannot reliably stop a live deepfake, a virtual camera or
  video replay that follows the turns. Identical twins may match.
- Anyone controlling this Windows account can register people or read templates.
- The match threshold needs calibrating on real faces with this camera.

## Left to the builder
OpenCV version (confirm YuNet/SFace work, otherwise pin 4.x), exact thresholds,
page styling, file layout.

## Done when
- Automated tests pass: unknown face, ambiguous match, wrong turn order, reused
  or expired challenge, two faces, lockout, removed person, expired session, normal user refused every admin
  action, admin action without a fresh face check, last admin protected, zip
  contains no personal data.
- On the real camera, the owner logs in; a printed or phone photo of the owner
  and a different person are refused.
