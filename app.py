"""Password + face login. Usage: python app.py run [--setup] | serve | list | remove NAME | passwd NAME"""
import argparse
import getpass
import os
import secrets
import sys
import threading
import time
import webbrowser
from functools import wraps

import waitress
from flask import Flask, abort, g, jsonify, request

import face
import store

# The pages in public/ are the same for everyone and hold no data. When hosted, Vercel serves them.
app = Flask(__name__, static_folder="public", static_url_path="")
# ORIGIN and PROXY_SECRET are set only when hosted: see `serve`.
app.config.update(TRUSTED_HOSTS=["127.0.0.1", "localhost"], MAX_CONTENT_LENGTH=512 * 1024,
                  ORIGIN=None, PROXY_SECRET=None)

# ponytail: in this process's memory, at most one per person. Move to the database if the app
# ever runs as several processes.
_challenges = {}
_lock = threading.Lock()  # also serializes password checks, so parallel guesses cannot dodge lockout
_tries = {}  # address -> times, when hosted
MAX_TRIES, TRY_WINDOW = 30, 300

BAD_LOGIN = "Wrong username or password."
LOCKED = "Too many failed tries. Wait 5 minutes."
BAD_INVITE = "This link has expired or was already used. Ask for a new one."
SEVERAL = "More than one face in view. Make sure only you can be seen."
REASONS = {"several faces": SEVERAL, "expired": "That took too long. Try again.",
           "face lost": "Your face left the picture. Try again.",
           "person changed": "The face changed part way. Try again.",
           "wrong turn": "You turned the other way. Try again."}


def same(a, b):
    return secrets.compare_digest(a.encode(), b.encode())


@app.before_request
def check_request():
    g.user = None
    if request.path == "/healthz":
        return
    origin = app.config["ORIGIN"]
    if origin and not same(request.headers.get("X-Origin-Secret", ""), app.config["PROXY_SECRET"]):
        abort(403)  # did not come through the frontend
    if request.method == "POST" and request.headers.get("Origin") != (origin or request.host_url.rstrip("/")):
        abort(403)
    g.token = request.cookies.get("session", "")
    row = store.get_session(g.token) if g.token else None
    g.user = dict(zip(("id", "name", "role"), row)) if row else None


@app.after_request
def headers(r):
    r.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Cache-Control"] = "no-store"
    return r


def is_admin():
    return bool(g.user) and g.user["role"] == "admin"


def admin_only(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not is_admin():
            abort(403)
        return f(*a, **kw)
    return wrapped


def too_many_tries():
    """Per address, when hosted. The frontend sets the first address; later ones are proxies."""
    if not app.config["ORIGIN"]:
        return False
    ip, now = request.headers.get("X-Forwarded-For", "").split(",")[0].strip(), time.time()
    with _lock:
        for k in [k for k, v in _tries.items() if v[-1] < now - TRY_WINDOW]:
            del _tries[k]
        recent = [t for t in _tries.get(ip, []) if t > now - TRY_WINDOW]
        if len(recent) >= MAX_TRIES:
            return True
        _tries[ip] = recent + [now]
    return False


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def error(message, code):
    return jsonify(status="error", message=message), code


# --- pages
@app.get("/", defaults={"page": "index"})
@app.get("/login", defaults={"page": "login"})
@app.get("/setup", defaults={"page": "setup"})
@app.get("/admin", defaults={"page": "admin"})
def page(page):
    return app.send_static_file(page + ".html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/me")
def me():
    return jsonify(user=g.user and {"name": g.user["name"], "role": g.user["role"]}, empty=not store.users())


def signed_out():
    r = jsonify(status="ok")
    r.delete_cookie("session")
    return r


@app.post("/api/logout")
def logout():
    if g.user:
        store.end_session(g.token)
        store.log("logout", True, g.user["name"])
    return signed_out()


@app.post("/api/me/delete")
def delete_me():
    if not g.user or not store.use_fresh(g.token):
        return error("Confirm with your face first.", 403)
    try:
        store.erase(g.user["id"], g.user["name"])
    except store.LastAdmin:
        return error("You are the last admin. Make someone else an admin first.", 400)
    store.log("erase", True)
    return signed_out()


@app.get("/api/admin")
@admin_only
def admin_data():
    return jsonify(users=[{"id": u.id, "name": u.name, "role": u.role} for u in store.users()],
                   history=[dict(zip(("at", "event", "ok", "name", "detail"), h)) for h in store.history()])


# --- admin actions: each uses up one fresh face check
def change_user(event, role=None):
    target = next((u for u in store.users() if u.id == body().get("id")), None)
    if target is None:
        return error("No such person.", 400)
    if not store.use_fresh(g.token):
        return error("Confirm with your face first.", 403)
    try:
        store.set_role(target.id, role) if role else store.remove_user(target.id)
    except store.LastAdmin:
        return error("The last admin cannot be removed or demoted.", 400)
    store.log(event, True, target.name, f"by {g.user['name']}" + (f", now {role}" if role else ""))
    return jsonify(status="ok")


@app.post("/api/admin/role")
@admin_only
def set_role():
    role = body().get("role")
    if role not in ("user", "admin"):
        return error("Unknown role.", 400)
    return change_user("role", role)


@app.post("/api/admin/remove")
@admin_only
def remove():
    return change_user("remove")


@app.post("/api/admin/invite")
@admin_only
def invite():
    role = body().get("role")
    if role not in ("user", "admin"):
        return error("Unknown role.", 400)
    if not store.use_fresh(g.token):
        return error("Confirm with your face first.", 403)
    key = secrets.token_urlsafe(32)
    store.add_invite(key, role, g.user["id"])
    store.log("invite", True, g.user["name"], role)
    return jsonify(status="ok", key=key)


# --- registering
def clean_name(name):
    name = name.strip() if isinstance(name, str) else ""
    return name if 1 <= len(name) <= 50 and name.isprintable() else None


def clean_password(password):
    return password if isinstance(password, str) and 8 <= len(password) <= 128 else None


def offer_setup(key):
    """Let the holder of this key create the first admin, while there is none."""
    if not any(u.role == "admin" for u in store.users()):
        store.add_invite(key, "admin", ttl=10 * 365 * 86400)


def start_registration(data):
    """The new person does the same head turns as at login, and the face seen is what is saved."""
    name, password, key = clean_name(data.get("name")), clean_password(data.get("password")), data.get("key")
    if not isinstance(key, str) or not store.invite_ok(key):
        return error(BAD_INVITE, 403)
    if not name:
        return error("Enter a username of up to 50 characters.", 400)
    if not password:
        return error("The password must be 8 to 128 characters.", 400)
    if store.find(name):
        return error("That username is already registered.", 400)
    pw = store.hash_password(password)
    slot = "register " + store._hash(key)  # one at a time per invite
    with _lock:
        cur = dict(id=secrets.token_urlsafe(32), purpose="register", slot=slot, token=None, name=name,
                   pw=pw, key=key, ch=face.Challenge(90, face.MIN_FACE))
        _challenges[slot] = cur
        return jsonify(id=cur["id"], prompt=cur["ch"].prompt)


def finish_registration(cur, result):
    ch, problem, role = cur["ch"], None, None
    if result != "ok":
        problem = REASONS[ch.reason]
    elif face.is_duplicate(ch.vec, store.users()):
        problem = "That face is already registered."
    else:
        try:
            role = store.redeem(cur["key"], cur["name"], ch.vec, cur["pw"])
        except store.NameTaken:
            problem = "That username is already registered."
        except store.InviteGone:
            problem = BAD_INVITE
    store.log("register", not problem, cur["name"], problem or role)
    return jsonify(status="fail", message=problem) if problem else jsonify(status="ok")


# --- face checks
@app.post("/api/challenge")
def start_challenge():
    if too_many_tries():
        return error(LOCKED, 429)
    data = body()
    purpose = data.get("purpose")
    if purpose == "login":
        name, password = clean_name(data.get("name")), clean_password(data.get("password"))
        if not name or not password:
            return error(BAD_LOGIN, 401)
    elif purpose == "stepup":
        if not g.user:
            return error("Log in first.", 403)
        name = g.user["name"]
    elif purpose == "register":
        return start_registration(data)
    else:
        return error("Unknown request.", 400)
    with _lock:
        user = store.find(name)
        name = user.name if user else name
        if store.locked(name):
            return error(LOCKED, 429)
        if purpose == "login" and not store.password_ok(user, password):
            store.log("login", False, name, "wrong password" if user else "unknown username")
            return error(BAD_LOGIN, 401)
        if not user:
            return error("Log in first.", 403)  # removed a moment ago
        old = _challenges.pop(user.id, None)
        # Walking away after seeing a prompt is a failed try, or the lockout could be dodged.
        if old and old["ch"].anchored:
            store.log(old["purpose"], False, name, "abandoned")
            if store.locked(name):
                return error(LOCKED, 429)
        # 90 s, because the browser may first ask the person to allow the camera.
        cur = dict(id=secrets.token_urlsafe(32), purpose=purpose, slot=user.id, pw=user.pw,
                   token=g.token if purpose == "stepup" else None, ch=face.Challenge(90))
        _challenges[user.id] = cur
        return jsonify(id=cur["id"], prompt=cur["ch"].prompt)


@app.post("/api/frame/<cid>")
def frame(cid):
    with _lock:
        cur = next((c for c in _challenges.values()
                    if secrets.compare_digest(c["id"].encode(), cid.encode())), None)
    if not cur or (cur["token"] and (cur["token"] != g.token or not g.user)):
        abort(404)
    faces = face.analyze(request.get_data())
    with _lock:
        if _challenges.get(cur["slot"]) is not cur:
            abort(404)
        result = cur["ch"].feed(faces)
        if result == "continue":
            return jsonify(status="continue", prompt=cur["ch"].prompt, seen=bool(faces),
                           small=bool(faces) and faces[0].size < cur["ch"].min_face)
        del _challenges[cur["slot"]]
        return (finish_registration if cur["purpose"] == "register" else finish)(cur, result)


def finish(cur, result):
    """Is this the person whose password was entered, or whose session this is?"""
    ch, purpose, user = cur["ch"], cur["purpose"], store.get(cur["slot"])
    ok, detail = False, ch.reason
    if user and user.pw != cur["pw"]:
        detail = "password changed"
    elif user and result == "ok":
        ok, detail = face.is_match(ch.vec, user), f"score {float(ch.vec @ user.vec):.2f}"
    store.log(purpose, ok, user.name if user else None, detail)
    if not ok:
        # How many faces are in view says nothing about who is registered. Other reasons stay private.
        return jsonify(status="fail",
                       message=SEVERAL if ch.reason == "several faces" else "Face not recognized.")
    r = jsonify(status="ok")
    if purpose == "stepup":
        store.make_fresh(g.token)
    else:
        if g.token:
            store.end_session(g.token)
        r.set_cookie("session", store.new_session(user.id), httponly=True, samesite="Strict",
                     secure=bool(app.config["ORIGIN"]))
    return r


# --- command line
def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("--setup", action="store_true", help="add another admin")
    sub.add_parser("serve", help="hosted: settings come from the environment")
    sub.add_parser("list")
    sub.add_parser("remove").add_argument("name")
    sub.add_parser("passwd").add_argument("name")
    a = p.parse_args(argv)

    if a.cmd == "run":
        url = "http://127.0.0.1:5000/"
        if a.setup or not any(u.role == "admin" for u in store.users()):
            key = secrets.token_urlsafe(32)
            store.add_invite(key, "admin")
            url += "setup#key=" + key
        print("Open", url, flush=True)
        opener = threading.Timer(1, webbrowser.open, [url])
        opener.daemon = True
        opener.start()
        waitress.serve(app, host="127.0.0.1", port=5000, threads=8)
        return
    if a.cmd == "serve":
        env = os.environ.get
        short = [k for k in ("PROXY_SECRET", "TEMPLATE_KEY") if len(env(k, "")) < 32]
        missing = [k for k in ("PUBLIC_ORIGIN", "DATABASE_URL") if not env(k)]
        if short or missing or 0 < len(env("SETUP_KEY", "")) < 32:
            sys.exit(f"Set {', '.join(missing + short) or 'SETUP_KEY'}. Secrets need at least 32 characters.")
        app.config.update(ORIGIN=env("PUBLIC_ORIGIN").rstrip("/"), PROXY_SECRET=env("PROXY_SECRET"),
                          TRUSTED_HOSTS=None)
        if env("SETUP_KEY"):
            offer_setup(env("SETUP_KEY"))
        waitress.serve(app, host="0.0.0.0", port=int(env("PORT", "10000")), threads=8)
        return
    if a.cmd == "list":
        for u in store.users():
            print(f"{u.role:6} {u.name}")
        return
    target = store.find(a.name.strip())
    if not target:
        sys.exit("No such person.")
    if a.cmd == "remove":
        try:
            store.remove_user(target.id)
        except store.LastAdmin:
            sys.exit("The last admin cannot be removed. Register another admin first.")
        store.log("remove", True, target.name, "by command line")
        print(f"Removed {target.name}.")
    elif a.cmd == "passwd":
        password = clean_password(getpass.getpass("New password: "))
        if not password:
            sys.exit("The password must be 8 to 128 characters.")
        if getpass.getpass("Again: ") != password:
            sys.exit("The passwords differ.")
        store.set_password(target.id, password)
        store.log("passwd", True, target.name, "by command line")
        print(f"Password changed for {target.name}.")


if __name__ == "__main__":
    main()
