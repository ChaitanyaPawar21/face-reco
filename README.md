# Password + face login

Register with a username, a password and the camera: it takes the person through a few head
turns and saves their face. To log in they enter the username and password. Only if those are
right does the camera turn on, repeat the head turns and compare the face with the saved one.
Two roles: admin and normal user.

## Start
Needs Windows, Python 3.13 and a camera. Double-click `run.bat`, or run it from a terminal.
It installs what it needs and opens the app. The first time, that is a one-use page to
create the first admin. Admins invite everyone else from the Admin page, with a link that works
once within 24 hours. Anyone can delete their own account and face data from the home page.

Only the person being registered or logging in may be in view: the check stops if the camera
sees a second face, such as a person, poster or screen behind them.

## Commands
Run in this folder, after `run.bat` has run once:

    .venv\Scripts\python app.py run            (Ctrl+C stops it)
    .venv\Scripts\python app.py run --setup    (open the page that adds another admin)
    .venv\Scripts\python app.py list
    .venv\Scripts\python app.py passwd "Name"
    .venv\Scripts\python app.py remove "Name"
    .venv\Scripts\python -m pytest

If the only admin's face stops matching, the same face cannot be registered twice: add another
person as admin with `run --setup`, then `remove` and register the first one again.

## Data
Passwords are hashed (scrypt). Face templates are encrypted for your Windows account and
kept in `%LOCALAPPDATA%\FaceLogin`. Camera pictures are never saved and nothing
leaves this PC. Get people's consent before registering their faces.

## Limits: read before relying on it
- The face check alone was beaten in testing: one ordinary photo, warped by software to
  imitate head turns and fed in as a fake camera, passed. A deepfake may too. The password
  is now needed as well, but someone with both the password and a photo may get in.
- Whoever opens an invite link signs up with their own face. Send it privately.
- Anyone who controls this Windows account can add admins or read the data.
- Five failed tries pause that username for 5 minutes, so someone can lock a person out on
  purpose. There is no limit per network address.
- Match thresholds are starting points. Compare the scores in Admin > History for the
  right and wrong people, then adjust the values at the top of `face.py`.
- `run` serves this PC only. Do not expose it to a network; use the hosted setup below.
- Hosted: whoever controls the Render account can read the face templates. Each address is limited
  to 30 tries in 5 minutes. Use it as a demo, not to protect anything sensitive.

## Hosting for free: Vercel (pages) + Render (API) + Neon (database)
Not used when running on a PC. Vercel serves `public/` and forwards `/api` to Render with a secret
header; Render refuses anything without it. Free Render sleeps after 15 idle minutes and takes about
a minute to wake. Set these, with long random secrets (at least 32 characters):

| Where | Setting | Value |
|---|---|---|
| Render | `DATABASE_URL` | Neon connection string, ending in `sslmode=require` |
| Render | `PUBLIC_ORIGIN` | `https://yourapp.vercel.app` |
| Render and Vercel | `PROXY_SECRET` | the same random value in both |
| Render | `TEMPLATE_KEY` | random. Keep a copy: without it everyone must sign up again |
| Render | `SETUP_KEY` | random. Open `https://yourapp.vercel.app/setup#key=SETUP_KEY` to create the first admin |

In `vercel.json`, replace `REPLACE-WITH-RENDER-HOST` with the Render host name. Run one Render instance only.

To run the tests on Postgres as well, set `TEST_DATABASE_URL`. They DROP its tables: never use the real one.

Models: YuNet (MIT) and SFace (Apache 2.0), from the OpenCV Zoo.
