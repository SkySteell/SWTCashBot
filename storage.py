import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
DB_PATH = os.getenv("DB_PATH", "data/shift_bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    litres INTEGER NOT NULL DEFAULT 0,
    backup INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER,
    paid INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plans(
    user_id INTEGER NOT NULL,
    ym TEXT NOT NULL,
    planned INTEGER NOT NULL,
    PRIMARY KEY(user_id, ym)
);
CREATE TABLE IF NOT EXISTS shifts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    start_odo REAL NOT NULL,
    end_odo REAL,
    yandex REAL,
    orders INTEGER,
    reminded TEXT
);
CREATE INDEX IF NOT EXISTS ix_shifts_user ON shifts(user_id, start_ts);
CREATE TABLE IF NOT EXISTS entries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT,
    litres REAL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entries_shift ON entries(shift_id);
"""

_lock = threading.Lock()
_conn = None
_path = DB_PATH


def init(path=None):
    global _conn, _path
    path = path or DB_PATH
    _path = path
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()
        cols = {r["name"] for r in _conn.execute("PRAGMA table_info(users)")}
        for name, ddl in (("backup", "INTEGER NOT NULL DEFAULT 0"), ("first_seen", "INTEGER"),
                          ("paid", "INTEGER NOT NULL DEFAULT 0")):
            if name not in cols:  # база создана старой версией
                _conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        _conn.commit()


def q(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchall()


def ex(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


# ---------- время ----------
def now():
    return datetime.now(TZ)


def ts(d):
    return int(d.timestamp())


def dt(t):
    return datetime.fromtimestamp(t, TZ)


def month_key(t):
    return dt(t).strftime("%Y-%m")


def month_bounds(ym):
    y, m = map(int, ym.split("-"))
    a = datetime(y, m, 1, tzinfo=TZ)
    b = datetime(y + (1 if m == 12 else 0), m % 12 + 1, 1, tzinfo=TZ)
    return ts(a), ts(b)


def year_bounds(y):
    return ts(datetime(y, 1, 1, tzinfo=TZ)), ts(datetime(y + 1, 1, 1, tzinfo=TZ))


# ---------- настройки ----------
def get_litres(uid):
    r = q("SELECT litres FROM users WHERE user_id=?", (uid,))
    return bool(r[0]["litres"]) if r else False


def set_litres(uid, on):
    ex("INSERT INTO users(user_id, litres) VALUES(?, ?) "
       "ON CONFLICT(user_id) DO UPDATE SET litres=excluded.litres", (uid, int(on)))


def get_backup(uid):
    r = q("SELECT backup FROM users WHERE user_id=?", (uid,))
    return bool(r[0]["backup"]) if r else False


def set_backup(uid, on):
    ex("INSERT INTO users(user_id, backup) VALUES(?, ?) "
       "ON CONFLICT(user_id) DO UPDATE SET backup=excluded.backup", (uid, int(on)))


def backup_users():
    return [r["user_id"] for r in q("SELECT user_id FROM users WHERE backup=1")]


def get_user(uid):
    r = q("SELECT * FROM users WHERE user_id=?", (uid,))
    return r[0] if r else None


def touch_user(uid):
    """Запоминает момент первого обращения (с него идёт пробный период)."""
    ex("INSERT INTO users(user_id, first_seen) VALUES(?, ?) "
       "ON CONFLICT(user_id) DO UPDATE SET first_seen=COALESCE(users.first_seen, excluded.first_seen)",
       (uid, ts(now())))


def set_paid(uid, on):
    ex("INSERT INTO users(user_id, paid) VALUES(?, ?) "
       "ON CONFLICT(user_id) DO UPDATE SET paid=excluded.paid", (uid, int(on)))


def all_users():
    return q("SELECT * FROM users WHERE first_seen IS NOT NULL OR paid=1")


def get_plan(uid, ym):
    r = q("SELECT planned FROM plans WHERE user_id=? AND ym=?", (uid, ym))
    return r[0]["planned"] if r else None


def set_plan(uid, ym, n):
    ex("INSERT INTO plans(user_id, ym, planned) VALUES(?,?,?) "
       "ON CONFLICT(user_id, ym) DO UPDATE SET planned=excluded.planned", (uid, ym, n))


# ---------- смены ----------
def open_shift(uid):
    r = q("SELECT * FROM shifts WHERE user_id=? AND end_ts IS NULL ORDER BY start_ts DESC LIMIT 1", (uid,))
    return r[0] if r else None


def get_shift(sid):
    r = q("SELECT * FROM shifts WHERE id=?", (sid,))
    return r[0] if r else None


def create_shift(uid, start_odo):
    return ex("INSERT INTO shifts(user_id, start_ts, start_odo) VALUES(?,?,?)",
              (uid, ts(now()), start_odo))


def close_shift(sid, end_odo, yandex, orders):
    ex("UPDATE shifts SET end_ts=?, end_odo=?, yandex=?, orders=? WHERE id=?",
       (ts(now()), end_odo, yandex, orders, sid))


SHIFT_FIELDS = {"start_odo", "end_odo", "yandex", "orders", "start_ts", "end_ts"}


def update_shift(sid, field, value):
    if field not in SHIFT_FIELDS:
        raise ValueError(field)
    ex(f"UPDATE shifts SET {field}=? WHERE id=?", (value, sid))


def shifts_between(uid, a, b, closed=True):
    extra = " AND end_ts IS NOT NULL" if closed else ""
    return q(f"SELECT * FROM shifts WHERE user_id=? AND start_ts>=? AND start_ts<?{extra} "
             "ORDER BY start_ts", (uid, a, b))


def open_shifts_unreminded(day):
    return q("SELECT * FROM shifts WHERE end_ts IS NULL AND (reminded IS NULL OR reminded<>?)", (day,))


def mark_reminded(sid, day):
    ex("UPDATE shifts SET reminded=? WHERE id=?", (day, sid))


def periods(uid):
    """Месяцы и годы, в которых есть закрытые смены (новые первыми)."""
    rows = q("SELECT start_ts FROM shifts WHERE user_id=? AND end_ts IS NOT NULL", (uid,))
    months = sorted({month_key(r["start_ts"]) for r in rows}, reverse=True)
    years = sorted({int(m[:4]) for m in months}, reverse=True)
    return months, years


# ---------- записи ----------
def add_entry(sid, uid, kind, amount, note=None, litres=None):
    return ex("INSERT INTO entries(shift_id, user_id, kind, amount, note, litres, ts) VALUES(?,?,?,?,?,?,?)",
              (sid, uid, kind, amount, note, litres, ts(now())))


def get_entry(eid):
    r = q("SELECT * FROM entries WHERE id=?", (eid,))
    return r[0] if r else None


def update_entry(eid, amount):
    ex("UPDATE entries SET amount=? WHERE id=?", (amount, eid))


def delete_entry(eid):
    ex("DELETE FROM entries WHERE id=?", (eid,))


def entries_for(shift_ids):
    out = {}
    ids = list(shift_ids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        for r in q(f"SELECT * FROM entries WHERE shift_id IN ({marks}) ORDER BY ts, id", part):
            out.setdefault(r["shift_id"], []).append(r)
    return out


# ---------- копия и восстановление ----------
def backup_bytes():
    """Согласованная копия всей базы (файл SQLite) в виде байтов."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with _lock:
            dst = sqlite3.connect(tmp)
            _conn.backup(dst)
            dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


def restore_from(path):
    """Заменяет базу файлом path. Возвращает False, если файл не похож на нашу базу."""
    check = None
    try:
        check = sqlite3.connect(path)
        names = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ok = {"users", "plans", "shifts", "entries"} <= names
        if ok:
            check.execute("SELECT COUNT(*) FROM shifts").fetchone()
    except sqlite3.DatabaseError:
        ok = False
    finally:
        if check:
            check.close()
    if not ok:
        return False
    global _conn
    with _lock:
        _conn.close()
        shutil.copyfile(path, _path)
    init(_path)
    return True
