"""Слой хранения: SQLite + функции доступа к данным.

Используется стандартная библиотека sqlite3 (без внешних ORM) — это минимизирует
зависимости и делает поведение предсказуемым. Каждый вызов открывает собственное
соединение, что безопасно при работе из нескольких потоков (планировщик + веб).
"""
import os
import sqlite3
import datetime as dt
from contextlib import contextmanager

from . import config, crypto

# Ключи настроек, значения которых хранятся в зашифрованном виде.
SECRET_SETTING_KEYS = {"telegram_token", "deepl_api_key", "github_token", "libretranslate_api_key"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@contextmanager
def get_conn():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                totp_secret TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                url TEXT NOT NULL,
                icon TEXT,
                auth_type TEXT NOT NULL DEFAULT 'link',
                username_enc TEXT,
                secret_enc TEXT,
                login_url TEXT,
                username_field TEXT,
                password_field TEXT,
                verify_tls INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                image_ref TEXT NOT NULL,
                registry TEXT NOT NULL,
                namespace TEXT NOT NULL,
                repository TEXT NOT NULL,
                tag TEXT NOT NULL DEFAULT 'latest',
                tag_filter TEXT,
                track_strategy TEXT NOT NULL DEFAULT 'latest_digest',
                enabled INTEGER NOT NULL DEFAULT 1,
                repo_url TEXT,
                last_digest TEXT,
                last_version TEXT,
                last_checked TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS update_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                old_version TEXT,
                new_version TEXT,
                old_digest TEXT,
                new_digest TEXT,
                notes_original TEXT,
                notes_ru TEXT,
                release_url TEXT,
                detected_at TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                detail TEXT,
                ip TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


# ---------------------------------------------------------------- users -----
def get_user_by_login(login: str):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()


def get_user(user_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def create_user(login: str, password_hash: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (login, password_hash, created_at) VALUES (?, ?, ?)",
            (login, password_hash, _now()),
        )
        return cur.lastrowid


def update_password(user_id: int, password_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
        )


# ------------------------------------------------------------- settings -----
def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    value = row["value"]
    if key in SECRET_SETTING_KEYS:
        return crypto.decrypt(value)
    return value


def set_setting(key: str, value: str) -> None:
    stored = crypto.encrypt(value) if key in SECRET_SETTING_KEYS else value
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, stored),
        )


def all_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = {}
    for r in rows:
        if r["key"] in SECRET_SETTING_KEYS:
            out[r["key"]] = crypto.decrypt(r["value"])
        else:
            out[r["key"]] = r["value"]
    return out


# ------------------------------------------------------------ resources -----
def list_resources():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM resources ORDER BY sort_order, name"
        ).fetchall()


def get_resource(rid: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM resources WHERE id = ?", (rid,)).fetchone()


def create_resource(data: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO resources
            (name, description, url, icon, auth_type, username_enc, secret_enc,
             login_url, username_field, password_field, verify_tls, sort_order, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["name"], data.get("description"), data["url"], data.get("icon"),
                data.get("auth_type", "link"),
                crypto.encrypt(data.get("username")),
                crypto.encrypt(data.get("secret")),
                data.get("login_url"), data.get("username_field"),
                data.get("password_field"),
                1 if data.get("verify_tls", True) else 0,
                int(data.get("sort_order", 0)), _now(),
            ),
        )
        return cur.lastrowid


def update_resource(rid: int, data: dict) -> None:
    current = get_resource(rid)
    if current is None:
        return
    # Если поле секрета пустое — оставляем прежнее значение (не затираем).
    username_enc = (
        crypto.encrypt(data["username"]) if data.get("username") else current["username_enc"]
    )
    secret_enc = (
        crypto.encrypt(data["secret"]) if data.get("secret") else current["secret_enc"]
    )
    with get_conn() as conn:
        conn.execute(
            """UPDATE resources SET name=?, description=?, url=?, icon=?, auth_type=?,
               username_enc=?, secret_enc=?, login_url=?, username_field=?,
               password_field=?, verify_tls=?, sort_order=? WHERE id=?""",
            (
                data["name"], data.get("description"), data["url"], data.get("icon"),
                data.get("auth_type", "link"), username_enc, secret_enc,
                data.get("login_url"), data.get("username_field"),
                data.get("password_field"),
                1 if data.get("verify_tls", True) else 0,
                int(data.get("sort_order", 0)), rid,
            ),
        )


def delete_resource(rid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM resources WHERE id = ?", (rid,))


def resource_credentials(resource) -> tuple[str, str]:
    """Возвращает расшифрованные (логин, секрет) ресурса."""
    return crypto.decrypt(resource["username_enc"]), crypto.decrypt(resource["secret_enc"])


# ------------------------------------------------------------- projects -----
def list_projects(enabled_only: bool = False):
    q = "SELECT * FROM projects"
    if enabled_only:
        q += " WHERE enabled = 1"
    q += " ORDER BY name"
    with get_conn() as conn:
        return conn.execute(q).fetchall()


def get_project(pid: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()


def create_project(data: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO projects
            (name, description, image_ref, registry, namespace, repository, tag,
             tag_filter, track_strategy, enabled, repo_url, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["name"], data.get("description"), data["image_ref"],
                data["registry"], data["namespace"], data["repository"],
                data.get("tag", "latest"), data.get("tag_filter"),
                data.get("track_strategy", "latest_digest"),
                1 if data.get("enabled", True) else 0,
                data.get("repo_url"), _now(),
            ),
        )
        return cur.lastrowid


def update_project(pid: int, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE projects SET name=?, description=?, image_ref=?, registry=?,
               namespace=?, repository=?, tag=?, tag_filter=?, track_strategy=?,
               enabled=?, repo_url=? WHERE id=?""",
            (
                data["name"], data.get("description"), data["image_ref"],
                data["registry"], data["namespace"], data["repository"],
                data.get("tag", "latest"), data.get("tag_filter"),
                data.get("track_strategy", "latest_digest"),
                1 if data.get("enabled", True) else 0,
                data.get("repo_url"), pid,
            ),
        )


def set_project_state(pid: int, digest: str, version: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET last_digest=?, last_version=?, last_checked=? WHERE id=?",
            (digest, version, _now(), pid),
        )


def touch_project_checked(pid: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE projects SET last_checked=? WHERE id=?", (_now(), pid))


def delete_project(pid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))


# --------------------------------------------------------- update_events ----
def create_event(data: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO update_events
            (project_id, old_version, new_version, old_digest, new_digest,
             notes_original, notes_ru, release_url, detected_at, notified)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                data["project_id"], data.get("old_version"), data.get("new_version"),
                data.get("old_digest"), data.get("new_digest"),
                data.get("notes_original"), data.get("notes_ru"),
                data.get("release_url"), _now(), 0,
            ),
        )
        return cur.lastrowid


def list_unnotified_events():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM update_events WHERE notified = 0 ORDER BY detected_at"
        ).fetchall()


def update_event_notes(event_id: int, notes_ru: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE update_events SET notes_ru=? WHERE id=?", (notes_ru, event_id)
        )


def mark_events_notified(event_ids: list[int]) -> None:
    if not event_ids:
        return
    with get_conn() as conn:
        conn.executemany(
            "UPDATE update_events SET notified = 1 WHERE id = ?",
            [(eid,) for eid in event_ids],
        )


def recent_events(limit: int = 100):
    with get_conn() as conn:
        return conn.execute(
            """SELECT e.*, p.name AS project_name, p.image_ref AS image_ref
               FROM update_events e LEFT JOIN projects p ON p.id = e.project_id
               ORDER BY e.detected_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


# -------------------------------------------------------------- audit -------
def log_action(action: str, detail: str = "", ip: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (action, detail, ip, created_at) VALUES (?,?,?,?)",
            (action, detail, ip, _now()),
        )


def recent_audit(limit: int = 50):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
