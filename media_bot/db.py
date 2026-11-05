"""SQLite state: jobs, dedupe cache, per-user preferences and download counters.

One guarded connection is enough for this workload and keeps deployment free of a
server database. Every write goes through ``self._lock`` because engine work runs in
worker threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    chat_id        INTEGER NOT NULL,
    url            TEXT NOT NULL,
    engine         TEXT NOT NULL DEFAULT '',
    platform       TEXT NOT NULL DEFAULT '',
    variant        TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending',
    progress       REAL NOT NULL DEFAULT 0,
    file_path      TEXT,
    size_bytes     INTEGER,
    error_code     TEXT,
    error_detail   TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_user_status ON jobs (user_id, status);
CREATE INDEX IF NOT EXISTS jobs_created     ON jobs (created_at);

CREATE TABLE IF NOT EXISTS dedupe (
    cache_key   TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    file_path   TEXT,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id         INTEGER PRIMARY KEY,
    default_variant TEXT NOT NULL DEFAULT 'best',
    notify_done     INTEGER NOT NULL DEFAULT 1,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cookie_files (
    user_id     INTEGER PRIMARY KEY,
    domains     TEXT NOT NULL DEFAULT '',
    entries     INTEGER NOT NULL DEFAULT 0,
    stored_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bundles (
    token       TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    engine      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS bundles_expiry ON bundles (expires_at);
"""

ACTIVE_STATUSES = ("pending", "queued", "running", "uploading")
DONE_STATUSES = ("done", "reused", "failed", "cancelled")


@dataclass(slots=True)
class Job:
    id: str
    user_id: int
    chat_id: int
    url: str
    engine: str
    platform: str
    variant: str
    status: str
    progress: float
    file_path: str | None
    size_bytes: int | None
    error_code: str | None
    error_detail: str | None
    created_at: float
    updated_at: float

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(**{key: row[key] for key in row.keys()})


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- jobs ----------------

    def create_job(self, *, user_id: int, chat_id: int, url: str, engine: str,
                   platform: str, variant: str) -> Job:
        now = time.time()
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, user_id, chat_id, url, engine, platform, variant,"
                " status, progress, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'queued',0,?,?)",
                (job_id, user_id, chat_id, url, engine, platform, variant, now, now),
            )
            self._conn.commit()
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "progress", "file_path", "size_bytes", "error_code",
                   "error_detail", "engine", "platform", "variant"}
        keys = [key for key in fields if key in allowed]
        if not keys:
            return
        assignments = ", ".join(f"{key}=?" for key in keys)
        values = [fields[key] for key in keys]
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {assignments}, updated_at=? WHERE id=?",
                (*values, time.time(), job_id),
            )
            self._conn.commit()

    def user_jobs(self, user_id: int, *, active_only: bool = True, limit: int = 20) -> list[Job]:
        query = "SELECT * FROM jobs WHERE user_id=?"
        if active_only:
            query += " AND status IN (%s)" % ",".join("?" * len(ACTIVE_STATUSES))
            params: tuple[Any, ...] = (user_id, *ACTIVE_STATUSES)
        else:
            params = (user_id,)
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [_row_to_job(row) for row in rows]

    def count_recent(self, user_id: int, *, window_seconds: int) -> int:
        since = time.time() - window_seconds
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE user_id=? AND created_at>=?",
                (user_id, since),
            ).fetchone()
        return int(row["n"])

    def forget_old(self, *, older_than_seconds: int) -> int:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE created_at<? AND status IN (%s)"
                % ",".join("?" * len(DONE_STATUSES)),
                (cutoff, *DONE_STATUSES),
            )
            self._conn.commit()
        return cursor.rowcount

    # ---------------- dedupe ----------------

    def dedupe_lookup(self, cache_key: str) -> tuple[str, str | None] | None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, file_path FROM dedupe WHERE cache_key=? AND expires_at>?",
                (cache_key, now),
            ).fetchone()
            if row:
                self._conn.execute("DELETE FROM dedupe WHERE expires_at<=?", (now,))
                self._conn.commit()
                return row["job_id"], row["file_path"]
            self._conn.execute("DELETE FROM dedupe WHERE expires_at<=?", (now,))
            self._conn.commit()
        return None

    def dedupe_store(self, cache_key: str, *, job_id: str, file_path: str | None,
                     ttl_seconds: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO dedupe (cache_key, job_id, file_path, expires_at)"
                " VALUES (?,?,?,?)",
                (cache_key, job_id, file_path, time.time() + ttl_seconds),
            )
            self._conn.commit()

    # ---------------- bundles ----------------
    # Telegram callback_data is capped at 64 bytes, so an inline button can only carry
    # a short token; the full variant list lives here.

    def save_bundle(self, token: str, *, url: str, engine: str, payload: dict[str, Any],
                    ttl_seconds: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO bundles (token, url, engine, payload, expires_at)"
                " VALUES (?,?,?,?,?)",
                (token, url, engine, json.dumps(payload, ensure_ascii=False),
                 time.time() + ttl_seconds),
            )
            self._conn.execute("DELETE FROM bundles WHERE expires_at<=?", (time.time(),))
            self._conn.commit()

    def load_bundle(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT url, engine, payload FROM bundles WHERE token=? AND expires_at>?",
                (token, time.time()),
            ).fetchone()
            self._conn.commit()
        if not row:
            return None
        data = json.loads(row["payload"])
        data.setdefault("url", row["url"])
        data.setdefault("engine", row["engine"])
        return data

    # ---------------- prefs / cookies ----------------

    def get_prefs(self, user_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT default_variant, notify_done FROM user_prefs WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if not row:
            return {"default_variant": "best", "notify_done": True}
        return {"default_variant": row["default_variant"], "notify_done": bool(row["notify_done"])}

    def set_pref(self, user_id: int, *, default_variant: str | None = None,
                 notify_done: bool | None = None) -> dict[str, Any]:
        current = self.get_prefs(user_id)
        if default_variant is not None:
            current["default_variant"] = default_variant
        if notify_done is not None:
            current["notify_done"] = notify_done
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO user_prefs (user_id, default_variant, notify_done, updated_at)"
                " VALUES (?,?,?,?)",
                (user_id, current["default_variant"], int(current["notify_done"]), time.time()),
            )
            self._conn.commit()
        return current

    def remember_cookies(self, user_id: int, *, domains: list[str], entries: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cookie_files (user_id, domains, entries, stored_at)"
                " VALUES (?,?,?,?)",
                (user_id, json.dumps(sorted(set(domains))), entries, time.time()),
            )
            self._conn.commit()

    def cookie_status(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT domains, entries, stored_at FROM cookie_files WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "domains": json.loads(row["domains"]),
            "entries": row["entries"],
            "stored_at": row["stored_at"],
        }

    def drop_cookies(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cookie_files WHERE user_id=?", (user_id,))
            self._conn.commit()


__all__ = ["Database", "Job", "ACTIVE_STATUSES", "DONE_STATUSES"]
