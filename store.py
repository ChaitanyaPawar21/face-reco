"""Storage: SQLite on a PC, Postgres when DATABASE_URL is set. Face templates are encrypted with
TEMPLATE_KEY when that is set, else with Windows DPAPI."""
import base64
import ctypes
import hashlib
import os
import secrets
import sqlite3
import threading
import time
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import psycopg
from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash

# Outside the project folder, in the Windows user's private profile.
DB = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FaceLogin" / "face.db"
URL = os.environ.get("DATABASE_URL")
IDLE, MAX_AGE, FRESH = 15 * 60, 8 * 3600, 120
MAX_FAILS, PAUSE = 5, 300
INVITE = 24 * 3600

User = namedtuple("User", "id name role vec pw")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','admin')), vec BLOB NOT NULL,
  pw TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS users_name ON users(lower(name));
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created DOUBLE PRECISION NOT NULL, seen DOUBLE PRECISION NOT NULL,
  fresh_until DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at DOUBLE PRECISION NOT NULL, event TEXT NOT NULL,
  ok INTEGER NOT NULL, name TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS invites(
  token TEXT PRIMARY KEY, role TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL,
  created_by INTEGER REFERENCES users(id) ON DELETE CASCADE);
"""
PG_SCHEMA = (SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
             .replace("BLOB", "BYTEA"))


class NameTaken(Exception):
    pass


class InviteGone(Exception):
    pass


class LastAdmin(Exception):
    pass


def db():
    """A transaction. Transactions run one at a time, on either database."""
    return _postgres() if URL else _sqlite()


@contextmanager
def _sqlite():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, isolation_level=None, timeout=10)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        if "pw" not in [r[1] for r in con.execute("PRAGMA table_info(users)")]:
            # Databases from the face-only version. An empty hash matches no password.
            con.execute("ALTER TABLE users ADD COLUMN pw TEXT NOT NULL DEFAULT ''")
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


_pg, _pg_lock = None, threading.Lock()


class _Pg:
    def execute(self, sql, args=()):
        return _pg.execute(sql.replace("?", "%s"), args)


@contextmanager
def _postgres():
    # ponytail: one kept-open connection, one transaction at a time. Use a pool if it gets busy.
    global _pg
    with _pg_lock:
        for last in (False, True):
            try:
                if _pg is None or _pg.closed:
                    _pg = psycopg.connect(URL, autocommit=True, connect_timeout=15, prepare_threshold=None)
                    _pg.execute(PG_SCHEMA)
                _pg.execute("BEGIN")
                _pg.execute("SELECT pg_advisory_xact_lock(1)")  # other processes, such as the CLI
                break
            except psycopg.OperationalError:  # the host closes idle connections
                if _pg:
                    _pg.close()
                if last:
                    raise
        try:
            yield _Pg()
            _pg.execute("COMMIT")
        except BaseException:
            try:
                _pg.execute("ROLLBACK")
            except psycopg.Error:
                _pg.close()
            raise


# --- encryption of face templates
if os.name == "nt":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    for _f in (_crypt32.CryptProtectData, _crypt32.CryptUnprotectData):
        _f.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        _f.restype = wintypes.BOOL
_UI_FORBIDDEN = 1


def _dpapi(name, data):
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = _Blob(len(data), ctypes.addressof(buf)), _Blob()
    if not getattr(_crypt32, name)(ctypes.byref(src), None, None, None, None, _UI_FORBIDDEN, ctypes.byref(dst)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        _kernel32.LocalFree(dst.data)


def _fernet():
    key = os.environ.get("TEMPLATE_KEY")  # a long random value, so a plain hash is enough
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())) if key else None


def protect(data):
    f = _fernet()
    return f.encrypt(data) if f else _dpapi("CryptProtectData", data)


def unprotect(blob):
    """Raises OSError if it cannot be read."""
    f = _fernet()
    if not f:
        return _dpapi("CryptUnprotectData", blob)
    try:
        return f.decrypt(blob)
    except InvalidToken as e:
        raise OSError("wrong TEMPLATE_KEY") from e


# --- users
def users(where="1=1", *args):
    with db() as con:
        rows = con.execute(f"SELECT id, name, role, vec, pw FROM users WHERE {where} ORDER BY name",
                           args).fetchall()
    out = []
    for i, name, role, blob, pw in rows:
        try:
            vec = np.frombuffer(unprotect(blob), np.float32)
        except OSError:
            vec = np.zeros(128, np.float32)  # unreadable template: listed, but matches nobody
        out.append(User(i, name, role, vec, pw))
    return out


def find(name):
    return next(iter(users("lower(name)=lower(?)", name)), None)


def get(user_id):
    return next(iter(users("id=?", user_id)), None)


hash_password = generate_password_hash
_DUMMY = hash_password(secrets.token_urlsafe())


def password_ok(user, password):
    """Takes as long for an unknown person as for a known one."""
    return check_password_hash((user and user.pw) or _DUMMY, password) and user is not None


def set_password(user_id, password):
    with db() as con:
        con.execute("UPDATE users SET pw=? WHERE id=?", (hash_password(password), user_id))
        con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def _insert(con, name, role, vec, pw):
    try:
        con.execute("INSERT INTO users(name, role, vec, pw) VALUES(?,?,?,?)",
                    (name, role, protect(np.asarray(vec, np.float32).tobytes()), pw))
    except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation) as e:
        raise NameTaken from e


def add_user(name, role, vec, pw):
    """pw is from hash_password. Raises NameTaken."""
    with db() as con:
        _insert(con, name, role, vec, pw)


def _keep_an_admin(con):
    if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        raise LastAdmin  # rolls the change back


def set_role(user_id, role):
    with db() as con:
        con.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        _keep_an_admin(con)


def remove_user(user_id):
    with db() as con:
        con.execute("DELETE FROM users WHERE id=?", (user_id,))
        _keep_an_admin(con)


def erase(user_id, name):
    """Remove a person and their name in the history."""
    with db() as con:
        con.execute("DELETE FROM users WHERE id=?", (user_id,))
        _keep_an_admin(con)
        con.execute("UPDATE audit SET name=NULL WHERE lower(name)=lower(?)", (name,))


# --- invites: one use, kept hashed, and only as long as whoever made one is an admin
_VALID = ("token=? AND expires>? AND (created_by IS NULL OR created_by IN "
          "(SELECT id FROM users WHERE role='admin'))")


def add_invite(key, role, by=None, ttl=INVITE):
    with db() as con:
        con.execute("DELETE FROM invites WHERE expires<? OR token=?", (time.time(), _hash(key)))
        con.execute("INSERT INTO invites(token, role, expires, created_by) VALUES(?,?,?,?)",
                    (_hash(key), role, time.time() + ttl, by))


def invite_ok(key):
    with db() as con:
        return con.execute(f"SELECT 1 FROM invites WHERE {_VALID}", (_hash(key), time.time())).fetchone() is not None


def redeem(key, name, vec, pw):
    """Use up an invite and add the person, or neither. Returns their role."""
    with db() as con:
        row = con.execute(f"DELETE FROM invites WHERE {_VALID} RETURNING role", (_hash(key), time.time())).fetchone()
        if not row:
            raise InviteGone
        _insert(con, name, row[0], vec, pw)
        return row[0]


# --- audit and lockout
def log(event, ok, name=None, detail=None):
    with db() as con:
        con.execute("INSERT INTO audit(at, event, ok, name, detail) VALUES(?,?,?,?,?)",
                    (time.time(), event, int(ok), name, detail))


def history(limit=100):
    with db() as con:
        return con.execute("SELECT at, event, ok, name, detail FROM audit ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()


def locked(name):
    """5 failures for a name (password or face) within 5 minutes pause it for 5 minutes."""
    with db() as con:
        t = [r[0] for r in con.execute(
            "SELECT at FROM audit WHERE ok=0 AND event IN ('login','stepup') "
            "AND lower(name)=lower(?) ORDER BY id DESC LIMIT ?", (name, MAX_FAILS))]
    return len(t) == MAX_FAILS and t[0] - t[-1] < PAUSE and time.time() < t[0] + PAUSE


# --- sessions
def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id):
    token, now = secrets.token_urlsafe(32), time.time()
    with db() as con:
        con.execute("INSERT INTO sessions(token, user_id, created, seen) VALUES(?,?,?,?)",
                    (_hash(token), user_id, now, now))
    return token


def get_session(token):
    """(id, name, role) of a live session, with the role as it is right now, else None."""
    now = time.time()
    with db() as con:
        con.execute("DELETE FROM sessions WHERE seen < ? OR created < ?", (now - IDLE, now - MAX_AGE))
        con.execute("UPDATE sessions SET seen=? WHERE token=?", (now, _hash(token)))
        return con.execute("SELECT u.id, u.name, u.role FROM sessions s JOIN users u ON u.id=s.user_id "
                           "WHERE s.token=?", (_hash(token),)).fetchone()


def end_session(token):
    with db() as con:
        con.execute("DELETE FROM sessions WHERE token=?", (_hash(token),))


def make_fresh(token):
    with db() as con:
        con.execute("UPDATE sessions SET fresh_until=? WHERE token=?", (time.time() + FRESH, _hash(token)))


def use_fresh(token):
    """True once per fresh face check."""
    with db() as con:
        return con.execute("UPDATE sessions SET fresh_until=0 WHERE token=? AND fresh_until>?",
                           (_hash(token), time.time())).rowcount == 1
