import json
import os
import sqlite3

import numpy as np
import pytest

import app as webapp
import face
import store
from test_face import YAW, unit

ADMIN, USER, STRANGER = unit(0), unit(1), unit(2)
ORIGIN = {"Origin": "http://127.0.0.1:5000"}
START = 1_000_000.0
PW = "correct horse"
PEOPLE = {0: "Ada", 1: "Uma"}
# Run the suite on Postgres too: set TEST_DATABASE_URL. Its tables are DROPPED by every test.
PG = os.environ.get("TEST_DATABASE_URL")
sqlite_only = pytest.mark.skipif(bool(PG), reason="looks inside the SQLite file")


class Clock:
    def __init__(self):
        self.t = START

    def time(self):
        return self.t


@pytest.fixture
def clock(monkeypatch, tmp_path):
    c = Clock()
    monkeypatch.setattr(store, "URL", PG)
    monkeypatch.setattr(store, "DB", tmp_path / "t.db")
    if PG:
        import psycopg
        with psycopg.connect(PG) as con:
            con.execute("DROP TABLE IF EXISTS invites, sessions, audit, users")
        if store._pg:
            store._pg.close()
    monkeypatch.setattr(store.time, "time", c.time)  # the one `time` module, shared by all
    monkeypatch.setattr(webapp, "_challenges", {})
    monkeypatch.setattr(webapp, "_tries", {})
    # Fake camera: the "jpeg" is a JSON list of [axis, yaw].
    monkeypatch.setattr(face, "analyze",
                        lambda b: [face.Face(unit(i), y) for i, y in json.loads(b)])
    store.add_user("Ada", "admin", ADMIN, store.hash_password(PW))
    store.add_user("Uma", "user", USER, store.hash_password(PW))
    return c


@pytest.fixture
def client(clock):
    return new_client()


def new_client():
    c = webapp.app.test_client()
    c.environ_base["HTTP_HOST"] = "127.0.0.1:5000"
    return c


def post(c, url, body=None, **kw):
    return c.post(url, json=body or {}, headers=ORIGIN, base_url="http://127.0.0.1:5000", **kw)


def get(c, url):
    return c.get(url, base_url="http://127.0.0.1:5000")


def me(c):
    """The name this client is logged in as, or None."""
    user = get(c, "/api/me").json["user"]
    return user and user["name"]


def frame(c, cid, faces):
    return c.post(f"/api/frame/{cid}", data=json.dumps(faces), headers=ORIGIN,
                  base_url="http://127.0.0.1:5000")


def challenge(c, axis, **body):
    """Run a whole challenge, following every prompt, as the person on this axis."""
    r = post(c, "/api/challenge", body)
    if r.status_code != 200:
        return r
    cid, prompt = r.json["id"], r.json["prompt"]
    while True:
        r = frame(c, cid, [[axis, YAW[prompt]]])
        if r.json.get("status") != "continue":
            return r
        prompt = r.json["prompt"]


def login(c, axis, name=None, password=PW):
    """Log in as `name` (the owner of that face unless given) showing the face on this axis."""
    return challenge(c, axis, purpose="login", name=name or PEOPLE[axis], password=password)


def start(c, name="Uma", password=PW):
    return post(c, "/api/challenge", {"purpose": "login", "name": name, "password": password})


def current():
    return next(iter(webapp._challenges.values()))


def stepup(c, axis=0):
    return challenge(c, axis, purpose="stepup")


def uid(name):
    return next(u.id for u in store.users() if u.name == name)


def names():
    return {u.name for u in store.users()}


# --- login
def test_pages_are_static_and_hold_no_data(client):
    for url in ("/", "/login", "/setup", "/admin"):
        r = get(client, url)
        assert r.status_code == 200 and b"Ada" not in r.data and b"Uma" not in r.data
    assert me(client) is None


def test_me_says_when_nobody_is_registered(client):
    assert get(client, "/api/me").json == {"user": None, "empty": False}
    for u in store.users():
        with store.db() as con:
            con.execute("DELETE FROM users WHERE id=?", (u.id,))
    assert get(client, "/api/me").json["empty"] is True


def test_registered_face_logs_in(client):
    assert login(client, 1).json["status"] == "ok"
    assert me(client) == "Uma"


def test_right_password_wrong_face_refused(client):
    for axis in (2, 0):  # a stranger, and another registered person
        r = login(client, axis, name="Uma")
        assert r.json == {"status": "fail", "message": "Face not recognized."}
        assert me(client) is None


@pytest.mark.parametrize("name,password", [
    ("Uma", "wrong password"), ("Nobody", PW), ("Uma", ""), ("Uma", None), ("", PW), (5, PW),
    ("Uma", ["x"]), ("Uma", "x" * 129)])
def test_bad_credentials_never_start_the_camera(client, name, password):
    r = start(client, name, password)
    assert r.status_code == 401 and r.json["message"] == "Wrong username or password."
    assert "id" not in r.json and not webapp._challenges


def test_login_without_credentials_refused(client):
    assert post(client, "/api/challenge", {"purpose": "login"}).status_code == 401


def test_username_ignores_case(client):
    assert login(client, 1, name=" uma ").json["status"] == "ok"


def test_two_people_can_log_in_at_once(client):
    other = new_client()
    a, b = start(client, "Uma").json, start(other, "Ada").json
    for c, axis, ch in ((client, 1, a), (other, 0, b)):
        assert frame(c, ch["id"], [[axis, 0]]).json["status"] == "continue"


def test_password_change_stops_login_in_progress_and_sessions(client):
    login(client, 1)
    other = new_client()
    r = start(other).json
    store.set_password(uid("Uma"), "a new password")
    assert me(client) is None
    cid, prompt = r["id"], r["prompt"]
    while (r := frame(other, cid, [[1, YAW[prompt]]]).json)["status"] == "continue":
        prompt = r["prompt"]
    assert r["status"] == "fail"
    assert login(other, 1).status_code == 401
    assert login(other, 1, password="a new password").json["status"] == "ok"


def test_removed_while_logging_in_refused(client):
    r = start(client).json
    store.remove_user(uid("Uma"))
    cid, prompt = r["id"], r["prompt"]
    while (r := frame(client, cid, [[1, YAW[prompt]]]).json)["status"] == "continue":
        prompt = r["prompt"]
    assert r["status"] == "fail"


def test_wrong_turn_gives_same_generic_message(client):
    cid = start(client).json["id"]
    r = frame(client, cid, [[1, 0]])
    wrong = {"left": -0.4, "right": 0.4}[r.json["prompt"]]
    assert frame(client, cid, [[1, wrong]]).json == {"status": "fail", "message": "Face not recognized."}


def test_challenge_cannot_be_reused(client):
    cid = start(client).json["id"]
    frame(client, cid, [[1, 0], [2, 0]])  # fails
    assert frame(client, cid, [[1, 0]]).status_code == 404
    assert frame(client, "made-up", [[1, 0]]).status_code == 404


def test_cookie_flags(client):
    cookie = login(client, 1).headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_forged_cookie_refused(client):
    client.set_cookie("session", "x" * 43, domain="127.0.0.1")
    assert me(client) is None


def test_logout(client):
    login(client, 1)
    token = client.get_cookie("session", domain="127.0.0.1").value
    post(client, "/api/logout")
    client.set_cookie("session", token, domain="127.0.0.1")
    assert me(client) is None


# --- lockout
def test_five_wrong_faces_pause_that_person_then_release(client, clock):
    for _ in range(5):
        assert login(client, 2, name="Uma").json["status"] == "fail"
    assert login(client, 1).status_code == 429
    clock.t += 299
    assert login(client, 1).status_code == 429
    clock.t += 2
    assert login(client, 1).json["status"] == "ok"


def test_five_wrong_passwords_pause_whatever_the_spelling(client):
    for name in ("Uma", "uma", "UMA", " Uma", "uMa"):
        assert start(client, name, "wrong password").status_code == 401
    assert start(client).status_code == 429


def test_one_persons_lockout_leaves_others_alone(client):
    for _ in range(5):
        start(client, "Uma", "wrong password")
    assert login(client, 0).json["status"] == "ok"


def test_lockout_does_not_reveal_who_is_registered(client):
    codes = {n: [start(client, n, "wrong password").status_code for _ in range(6)]
             for n in ("Uma", "Nobody")}
    assert codes["Uma"] == codes["Nobody"] == [401] * 5 + [429]


def test_abandoning_after_seeing_a_prompt_counts(client):
    for _ in range(5):
        frame(client, start(client).json["id"], [[2, 0]])  # prompt revealed, walk away
    assert start(client).status_code == 429


def test_success_does_not_reset_failures(client):
    for _ in range(4):
        login(client, 2, name="Uma")
    login(client, 1)
    login(client, 2, name="Uma")
    assert login(client, 1).status_code == 429


# --- sessions
def test_idle_expiry(client, clock):
    login(client, 1)
    clock.t += 899
    assert me(client) == "Uma"
    clock.t += 901
    assert me(client) is None


def test_absolute_expiry(client, clock):
    login(client, 1)
    for _ in range(50):
        clock.t += 600
        last = me(client)
    assert last is None  # 30000 s > 8 h despite activity


def test_removed_person_is_out_immediately(client):
    login(client, 1)
    store.remove_user(uid("Uma"))
    assert me(client) is None


# --- roles
def test_normal_user_refused_everywhere(client):
    login(client, 1)
    for _ in range(2):  # the second time with a fresh check of their own face
        assert get(client, "/api/admin").status_code == 403
        assert post(client, "/api/admin/invite", {"role": "admin"}).status_code == 403
        assert post(client, "/api/admin/role", {"id": uid("Uma"), "role": "admin"}).status_code == 403
        assert post(client, "/api/admin/remove", {"id": uid("Ada")}).status_code == 403
        assert stepup(client, axis=1).json["status"] == "ok"
    assert names() == {"Ada", "Uma"}


def test_anonymous_refused_everywhere(client):
    assert get(client, "/api/admin").status_code == 403
    assert post(client, "/api/challenge", {"purpose": "stepup"}).status_code == 403
    assert post(client, "/api/admin/invite", {"role": "user"}).status_code == 403
    assert post(client, "/api/admin/remove", {"id": uid("Uma")}).status_code == 403
    assert post(client, "/api/me/delete").status_code == 403
    assert register(client, 3).status_code == 403
    assert names() == {"Ada", "Uma"}


def test_every_route_is_accounted_for():
    """A new route must be added here on purpose, with its access rule."""
    assert {r.rule for r in webapp.app.url_map.iter_rules()} == {
        "/", "/login", "/admin", "/setup", "/healthz", "/<path:filename>",  # static, no data
        "/api/me", "/api/logout", "/api/challenge", "/api/frame/<cid>",
        "/api/me/delete",  # own face check
        "/api/admin", "/api/admin/role", "/api/admin/remove", "/api/admin/invite"}



def test_admin_sees_people_and_history(client):
    login(client, 0)
    r = get(client, "/api/admin").json
    assert [u["name"] for u in r["users"]] == ["Ada", "Uma"]
    assert all(set(u) == {"id", "name", "role"} for u in r["users"])
    assert r["history"][0]["event"] == "login"


def test_admin_actions_need_a_fresh_face_check(client):
    login(client, 0)
    assert post(client, "/api/admin/remove", {"id": uid("Uma")}).status_code == 403
    assert post(client, "/api/admin/role", {"id": uid("Uma"), "role": "admin"}).status_code == 403
    assert post(client, "/api/admin/invite", {"role": "user"}).status_code == 403
    assert names() == {"Ada", "Uma"}


def test_fresh_check_allows_one_action(client):
    login(client, 0)
    assert stepup(client).json["status"] == "ok"
    assert post(client, "/api/admin/role", {"id": uid("Uma"), "role": "admin"}).status_code == 200
    assert post(client, "/api/admin/remove", {"id": uid("Uma")}).status_code == 403


def test_fresh_check_expires(client, clock):
    login(client, 0)
    stepup(client)
    clock.t += 121
    assert post(client, "/api/admin/remove", {"id": uid("Uma")}).status_code == 403


def test_someone_elses_face_cannot_confirm(client):
    login(client, 0)
    assert stepup(client, axis=1).json["status"] == "fail"
    assert post(client, "/api/admin/remove", {"id": uid("Uma")}).status_code == 403


def test_another_session_cannot_use_my_challenge(client):
    login(client, 0)
    cid = post(client, "/api/challenge", {"purpose": "stepup"}).json["id"]
    assert frame(new_client(), cid, [[0, 0]]).status_code == 404


def test_demoted_admin_loses_access(client):
    login(client, 0)
    store.set_role(uid("Uma"), "admin")
    store.set_role(uid("Ada"), "user")
    assert get(client, "/api/admin").status_code == 403


def test_last_admin_protected(client):
    login(client, 0)
    stepup(client)
    assert post(client, "/api/admin/remove", {"id": uid("Ada")}).status_code == 400
    stepup(client)
    assert post(client, "/api/admin/role", {"id": uid("Ada"), "role": "user"}).status_code == 400
    assert next(u for u in store.users() if u.name == "Ada").role == "admin"


def test_bad_input(client):
    login(client, 0)
    for body in ({}, {"id": "x"}, {"id": 999}, {"id": uid("Uma"), "role": "root"}):
        stepup(client)
        assert post(client, "/api/admin/role", body).status_code == 400
    assert post(client, "/api/challenge", {"purpose": "nope"}).status_code == 400


# --- registering, with a one-use invite link, on the new person's own device
def invite(role="user", by=None, ttl=3600):
    store.add_invite("invite-key", role, by, ttl)
    return "invite-key"


def register(c, axis, name="New", password=PW, key="invite-key", **extra):
    return challenge(c, axis, purpose="register", name=name, password=password, key=key, **extra)


def begin(c, name="New"):
    return post(c, "/api/challenge", {"purpose": "register", "name": name, "password": PW, "key": "invite-key"}).json


def play_out(c, ch, axis):
    r = {"status": "continue", "prompt": ch["prompt"]}
    while r["status"] == "continue":
        r = frame(c, ch["id"], [[axis, YAW[r["prompt"]]]]).json
    return r


@pytest.mark.parametrize("role", ["user", "admin"])
def test_admin_invites_someone_who_signs_up_and_logs_in(client, role):
    login(client, 0)
    stepup(client)
    key = post(client, "/api/admin/invite", {"role": role}).json["key"]
    assert len(key) > 40
    other = new_client()
    assert register(other, 3, key=key, role="admin").json["status"] == "ok"  # the role asked for is ignored
    assert store.find("New").role == role
    assert login(other, 3, name="New").json["status"] == "ok"
    assert register(other, 4, name="Two", key=key).status_code == 403  # one use
    assert post(client, "/api/admin/invite", {"role": "user"}).status_code == 403  # one face check, one invite


@pytest.mark.parametrize("role", ["root", None, 5, ["user"]])
def test_bad_invite_role_refused_without_using_the_face_check(client, role):
    login(client, 0)
    stepup(client)
    assert post(client, "/api/admin/invite", {"role": role}).status_code == 400
    assert post(client, "/api/admin/invite", {"role": "user"}).status_code == 200


@pytest.mark.parametrize("key", ["", "wrong", "\u00e9", 5, None, ["x"]])
def test_bad_invite_refused(client, key):
    invite()
    assert register(client, 3, key=key).status_code == 403
    assert names() == {"Ada", "Uma"}


def test_expired_invite_refused(client, clock):
    invite()
    clock.t += 3601
    assert register(client, 3).status_code == 403


def test_invite_dies_with_the_admin_who_made_it(client):
    invite(by=uid("Ada"))
    store.set_role(uid("Uma"), "admin")
    ch = begin(client)
    store.set_role(uid("Ada"), "user")
    assert play_out(client, ch, 3)["status"] == "fail"  # already started
    assert register(client, 3).status_code == 403
    store.remove_user(uid("Ada"))
    assert register(client, 3).status_code == 403
    assert names() == {"Uma"}


def test_two_people_racing_one_invite(client):
    invite()
    a = begin(client, "First")
    b = begin(new_client(), "Second")
    assert frame(client, a["id"], [[3, 0]]).status_code == 404  # only the newest try runs
    assert play_out(client, b, 4)["status"] == "ok"
    assert names() == {"Ada", "Uma", "Second"}


def test_already_registered_face_refused_and_invite_kept(client):
    invite()
    r = register(client, 1).json
    assert r["status"] == "fail" and "face is already registered" in r["message"]
    assert register(client, 3).json["status"] == "ok"


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"name": "  "}, {"name": "x" * 51}, {"name": "a\nb"}, {"name": "uma"}, {"name": 5},
    {"password": ""}, {"password": "short"}, {"password": "x" * 129}, {"password": None}])
def test_bad_details_never_start_the_camera(client, bad):
    invite()
    assert register(client, 3, **bad).status_code == 400
    assert not webapp._challenges and names() == {"Ada", "Uma"}


def test_registering_needs_the_head_turns(client):
    invite()
    ch = begin(client)
    for _ in range(50):  # a still picture
        assert frame(client, ch["id"], [[3, 0]]).json["status"] == "continue"
    assert names() == {"Ada", "Uma"}


def test_name_taken_while_registering_is_refused_and_invite_kept(client):
    invite()
    ch = begin(client)
    store.add_user("new", "user", unit(5), store.hash_password(PW))
    r = play_out(client, ch, 3)
    assert r["status"] == "fail" and "username is already registered" in r["message"]
    assert register(client, 3, name="Other").json["status"] == "ok"


def test_failed_registration_says_why(client):
    invite()
    ch = begin(client)
    prompt = frame(client, ch["id"], [[3, 0]]).json["prompt"]
    assert "other way" in frame(client, ch["id"], [[3, -YAW[prompt]]]).json["message"]


def test_several_faces_message_is_shown_at_login(client):
    cid = start(client).json["id"]
    r = frame(client, cid, [[1, 0], [2, 0]]).json
    assert r["status"] == "fail" and "More than one face" in r["message"]
    assert me(client) is None


def test_registering_waits_for_a_close_face(client, monkeypatch):
    invite()
    ch = begin(client)
    monkeypatch.setattr(face, "analyze", lambda b: [face.Face(unit(3), 0, face.MIN_FACE - 1)])
    r = frame(client, ch["id"], []).json
    assert r["prompt"] == "center" and r["small"] is True
    assert not current()["ch"].anchored


def test_plain_password_is_not_kept_while_registering(client):
    invite()
    begin(client)
    assert PW not in repr(current())


# --- deleting your own data
def test_person_deletes_their_own_data(client):
    login(client, 1)
    assert post(client, "/api/me/delete").status_code == 403  # needs a fresh face check
    stepup(client, axis=1)
    assert post(client, "/api/me/delete").status_code == 200
    assert me(client) is None and names() == {"Ada"}
    assert not [h for h in store.history() if h[3] == "Uma"]


def test_last_admin_cannot_delete_themselves(client):
    login(client, 0)
    stepup(client)
    assert post(client, "/api/me/delete").status_code == 400
    assert names() == {"Ada", "Uma"} and me(client) == "Ada"


# --- first admin
def test_setup_key_works_only_while_there_is_no_admin(client):
    webapp.offer_setup("s" * 40)
    assert register(client, 3, key="s" * 40).status_code == 403  # Ada is an admin
    with store.db() as con:
        con.execute("DELETE FROM users")
    webapp.offer_setup("s" * 40)
    webapp.offer_setup("s" * 40)  # a restart
    assert register(client, 3, key="s" * 40).json["status"] == "ok"
    assert store.find("New").role == "admin"
    webapp.offer_setup("s" * 40)  # another restart
    assert register(client, 4, name="Two", key="s" * 40).status_code == 403


# --- hosted behind Vercel
WEB = {"Origin": "https://face.example", "X-Origin-Secret": "p" * 40, "X-Forwarded-For": "1.2.3.4, 5.6.7.8"}


@pytest.fixture
def hosted(client, monkeypatch):
    monkeypatch.setitem(webapp.app.config, "ORIGIN", "https://face.example")
    monkeypatch.setitem(webapp.app.config, "PROXY_SECRET", "p" * 40)
    monkeypatch.setitem(webapp.app.config, "TRUSTED_HOSTS", None)

    def send(body, **headers):
        return client.post("/api/challenge", json=body, headers={**WEB, **headers},
                           base_url="http://backend.example")
    send.client = client
    return send


LOGIN = {"purpose": "login", "name": "Uma", "password": PW}


def test_hosted_accepts_only_requests_through_the_frontend(hosted):
    assert hosted(LOGIN).status_code == 200
    for bad in ({"X-Origin-Secret": ""}, {"X-Origin-Secret": "wrong"}, {"X-Origin-Secret": "\u00e9"},
                {"Origin": "http://backend.example"}, {"Origin": "https://evil.example"}):
        assert hosted(LOGIN, **bad).status_code == 403
    assert hosted.client.get("/api/me", base_url="http://backend.example").status_code == 403
    assert hosted.client.get("/healthz", base_url="http://backend.example").status_code == 200


def test_hosted_cookie_is_secure(hosted):
    ch = hosted(LOGIN).json
    r = {"status": "continue", "prompt": ch["prompt"]}
    while r["status"] == "continue":
        res = hosted.client.post(f"/api/frame/{ch['id']}", data=json.dumps([[1, YAW[r["prompt"]]]]), headers=WEB,
                                 base_url="http://backend.example")
        r = res.json
    cookie = res.headers["Set-Cookie"]
    assert r["status"] == "ok" and all(w in cookie for w in ("Secure", "HttpOnly", "SameSite=Strict"))


def test_hosted_limits_each_address(hosted, clock):
    def attempt(ip, n):  # a different username each time, so no single one is locked
        return hosted({**LOGIN, "name": f"x{n}"}, **{"X-Forwarded-For": ip}).status_code
    assert [attempt("9.9.9.9", n) for n in range(31)] == [401] * 30 + [429]
    assert attempt("9.9.9.9, 1.1.1.1", 50) == 429  # what later proxies add is ignored
    assert attempt("8.8.8.8", 51) == 401
    clock.t += 301
    assert attempt("9.9.9.9", 52) == 401


def test_serve_refuses_to_start_half_configured(monkeypatch):
    for k in ("PUBLIC_ORIGIN", "PROXY_SECRET", "TEMPLATE_KEY", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="PROXY_SECRET"):
        webapp.main(["serve"])


def test_encryption_with_a_key(monkeypatch):
    monkeypatch.setenv("TEMPLATE_KEY", "k" * 40)
    blob = store.protect(b"secret")
    assert b"secret" not in blob and store.unprotect(blob) == b"secret"
    for other in ("j" * 40,):
        monkeypatch.setenv("TEMPLATE_KEY", other)
        with pytest.raises(OSError):
            store.unprotect(blob)


# --- request safety
@pytest.mark.parametrize("headers", [{}, {"Origin": "http://evil.example"}, {"Origin": "null"}])
def test_cross_site_post_refused(client, headers):
    r = client.post("/api/challenge", json={"purpose": "login", "name": "Uma", "password": PW}, headers=headers,
                    base_url="http://127.0.0.1:5000")
    assert r.status_code == 403


def test_foreign_host_refused(client):
    assert client.get("/login", base_url="http://evil.example").status_code == 400


def test_security_headers(client):
    h = get(client, "/api/me").headers
    assert "default-src 'self'" in h["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert h["Cache-Control"] == "no-store"


# --- storage
@sqlite_only
def test_templates_and_tokens_not_readable_on_disk(client):
    login(client, 1)
    token = client.get_cookie("session", domain="127.0.0.1").value
    raw = store.DB.read_bytes()
    assert USER.astype(np.float32).tobytes() not in raw
    assert token.encode() not in raw
    assert PW.encode() not in raw
    assert np.allclose(next(u.vec for u in store.users() if u.name == "Uma"), USER)


@sqlite_only
def test_no_images_stored(client):
    login(client, 1)
    db = sqlite3.connect(store.DB)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert tables - {"sqlite_sequence"} == {"users", "sessions", "audit", "invites"}


@sqlite_only
def test_unreadable_template_is_skipped_not_fatal(client):
    db = sqlite3.connect(store.DB)
    db.execute("UPDATE users SET vec = x'00' WHERE name = 'Uma'")
    db.commit()
    db.close()
    assert login(client, 1).json["status"] == "fail"
    assert login(client, 0).json["status"] == "ok"


@sqlite_only
def test_old_database_is_upgraded(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB", tmp_path / "old.db")
    db = sqlite3.connect(store.DB)
    db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE "
               "COLLATE NOCASE, role TEXT NOT NULL, vec BLOB NOT NULL)")
    db.execute("INSERT INTO users(name, role, vec) VALUES('Old', 'admin', x'00')")
    db.commit()
    db.close()
    old = store.find("Old")
    assert not any(store.password_ok(old, p) for p in ("", "None", PW))
    store.set_password(old.id, PW)
    assert store.password_ok(store.find("Old"), PW)


# --- command line
def test_cli_list_remove_passwd(clock, monkeypatch, capsys):
    webapp.main(["list"])
    assert "Uma" in capsys.readouterr().out
    monkeypatch.setattr(webapp.getpass, "getpass", lambda _: "a new password")
    webapp.main(["passwd", "uma"])
    assert store.password_ok(store.find("Uma"), "a new password")
    monkeypatch.setattr(webapp.getpass, "getpass", lambda _: "short")
    for args in (["passwd", "Uma"], ["passwd", "Nobody"], ["remove", "Nobody"], ["remove", "Ada"]):
        with pytest.raises(SystemExit):
            webapp.main(args)
    webapp.main(["remove", "Uma"])
    assert names() == {"Ada"}


@pytest.mark.parametrize("args,empty,opens", [([], False, False), ([], True, True), (["--setup"], False, True)])
def test_run_opens_setup_only_when_needed(clock, monkeypatch, capsys, args, empty, opens):
    served = {}
    monkeypatch.setattr(webapp.waitress, "serve", lambda app, **kw: served.update(kw))
    monkeypatch.setattr(webapp.threading.Timer, "start", lambda self: None)
    if empty:
        monkeypatch.setattr(store, "users", lambda *a: [])
    webapp.main(["run", *args])
    assert served["host"] == "127.0.0.1"
    assert opens == ("setup#key=" in capsys.readouterr().out)
