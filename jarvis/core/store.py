"""Local persistence: chat history, safe preferences and non-sensitive activity notes (SQLite, stdlib only).

Lives in %LOCALAPPDATA%\\Jarvis (override with JARVIS_DATA_DIR), outside the repository.

Failure policy is fail closed with a visible explanation, never silent loss:
  * unreadable/corrupt file   -> the file is PRESERVED under a .corrupt-<time> name, a fresh database is used
  * schema newer than this app -> the file is left untouched; this session runs in memory
  * migration fails            -> a .bak-v<N> copy was made first; the file is left at its last good version
  * folder/file unavailable    -> this session runs in memory
In every degraded case `notice` explains what happened and `persistent` is False when nothing is being saved.
"""
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from jarvis.core.providers import PROVIDER_NAMES
from jarvis.core.redact import redact

MAX_TEXT = 4000
MAX_MESSAGES = 1000
MAX_ACTIVITY = 500

# version -> statements that bring the schema to that version
MIGRATIONS = {
    1: [
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL)",
        "CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)",
    ],
}

# The only preferences that exist: safe values only, validated on write.
PREFS = {
    "provider": ("offline", lambda v: v in PROVIDER_NAMES),
    "save_history": (True, lambda v: isinstance(v, bool)),
    "reduce_motion": (False, lambda v: isinstance(v, bool)),
}


def default_data_dir() -> Path:
    override = os.environ.get("JARVIS_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Jarvis"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Store:
    def __init__(self, path: Path | None = None, migrations: dict | None = None):
        """path=None gives a throwaway in-memory store (no persistence, no notice)."""
        self.path = Path(path) if path else None
        self.migrations = migrations or MIGRATIONS
        self.latest = max(self.migrations)
        self.notice = ""
        self.persistent = False
        self.backup_path: Path | None = None
        self.quarantine_path: Path | None = None
        self._conn: sqlite3.Connection | None = None
        self._open()

    @classmethod
    def open_default(cls) -> "Store":
        return cls(default_data_dir() / "jarvis.db")

    # ---- opening, recovery, migration
    def _apply(self, conn: sqlite3.Connection, version: int) -> None:
        """Bring a connection from `version` to the latest schema; each step is all-or-nothing."""
        for v in range(version + 1, self.latest + 1):
            # explicit BEGIN: Python's sqlite3 does not open a transaction before DDL, so without it the first
            # statements of a failing step would stay applied
            conn.execute("BEGIN")
            try:
                for stmt in self.migrations[v]:
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {int(v)}")
                conn.execute("COMMIT")
            except BaseException:
                conn.rollback()
                raise

    def _memory(self, notice: str = "") -> None:
        """Throwaway in-memory database for this session. Never recurses: a failing migration just stops here."""
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.persistent = False
        if notice:
            self.notice = notice
        try:
            self._apply(self._conn, 0)
        except sqlite3.Error:
            pass

    def _open(self) -> None:
        if self.path is None:
            self._memory()
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = self._connect()  # assigned first so a damaged file's handle can be closed before renaming
            version = self._check(self._conn)
        except sqlite3.Error:
            self._quarantine_and_recreate()
            return
        except OSError as e:
            self._memory(f"The local data folder is unavailable ({type(e).__name__}); "
                         "history and settings will not be saved this session.")
            return
        if version > self.latest:
            self._memory(f"Local data at {self.path} was written by a newer version of Jarvis (schema {version}). "
                         "It was left untouched; history and settings will not be saved this session.")
            return
        self.persistent = True
        if version < self.latest:
            self._migrate_connection(self._conn, version, backup=self.path.stat().st_size > 0 and version > 0)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5, check_same_thread=False)

    @staticmethod
    def _check(conn: sqlite3.Connection) -> int:
        """Raises sqlite3.Error for unreadable or damaged files; returns the schema version."""
        verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
        if verdict != "ok":
            raise sqlite3.DatabaseError(f"integrity check failed: {verdict}")
        return conn.execute("PRAGMA user_version").fetchone()[0]

    def _quarantine_and_recreate(self) -> None:
        try:
            if self._conn:
                self._conn.close()
        except sqlite3.Error:
            pass
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.replace(self.path, dest)
            for suffix in ("-wal", "-shm", "-journal"):
                side = Path(str(self.path) + suffix)
                if side.exists():
                    os.replace(side, Path(str(dest) + suffix))
            self.quarantine_path = dest
            conn = self._connect()
            self._check(conn)
            self._conn, self.persistent = conn, True
            self._migrate_connection(conn, 0, backup=False)
            self.notice = (f"Your saved Jarvis data could not be read. The damaged file was kept at {dest} "
                           "and a new empty history was started.")
        except (OSError, sqlite3.Error) as e:
            self._memory(f"Your saved Jarvis data could not be read and could not be set aside "
                         f"({type(e).__name__}); history and settings will not be saved this session.")

    def _migrate_connection(self, conn: sqlite3.Connection, version: int, backup: bool) -> None:
        try:
            if backup and self.path is not None:
                self.backup_path = self.path.with_name(f"{self.path.name}.bak-v{version}")
                conn.commit()
                shutil.copy2(self.path, self.backup_path)
            self._apply(conn, version)
        except (sqlite3.Error, OSError) as e:
            at = self.conn_version(conn)
            self._memory(f"Updating your saved data failed ({type(e).__name__}) and it was left at schema {at}"
                         + (f"; a copy is at {self.backup_path}" if self.backup_path else "")
                         + ". History and settings will not be saved this session.")

    @staticmethod
    def conn_version(conn: sqlite3.Connection) -> int:
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error:
            return -1

    # ---- guarded execution
    def _run(self, sql: str, args=(), fetch=False, default=None):
        try:
            with self._conn:
                cur = self._conn.execute(sql, args)
                return cur.fetchall() if fetch else cur.rowcount
        except sqlite3.Error as e:
            self.notice = f"Local data could not be {'read' if fetch else 'saved'} ({type(e).__name__})."
            return default

    # ---- messages
    def add_message(self, role: str, text: str, ts: str | None = None) -> dict:
        text = redact(text)[:MAX_TEXT]
        ts = ts or now_iso()
        self._run("INSERT INTO messages (ts, role, text) VALUES (?, ?, ?)", (ts, role, text))
        self._run("DELETE FROM messages WHERE id <= (SELECT MAX(id) FROM messages) - ?", (MAX_MESSAGES,))
        return {"ts": ts, "role": role, "text": text}

    def messages(self, limit: int = 200) -> list[dict]:
        rows = self._run("SELECT ts, role, text FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id",
                         (limit,), fetch=True, default=[])
        return [{"ts": r[0], "role": r[1], "text": r[2]} for r in rows]

    def clear_messages(self) -> int:
        n = self._run("DELETE FROM messages") or 0
        self.add_activity("history_cleared", f"{n} message(s) deleted")
        return n

    # ---- preferences
    def get_pref(self, key: str):
        default, valid = PREFS[key]
        rows = self._run("SELECT value FROM preferences WHERE key = ?", (key,), fetch=True, default=[])
        if rows:
            try:
                value = json.loads(rows[0][0])
                if valid(value):
                    return value
            except ValueError:
                pass
        return default

    def set_pref(self, key: str, value) -> None:
        if key not in PREFS or not PREFS[key][1](value):
            raise ValueError(f"invalid preference {key!r}")
        self._run("INSERT INTO preferences (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    def reset_prefs(self) -> None:
        self._run("DELETE FROM preferences")
        self.add_activity("prefs_reset", "preferences restored to defaults")

    # ---- activity (non-sensitive notes only)
    def add_activity(self, kind: str, detail: str = "") -> None:
        self._run("INSERT INTO activity (ts, kind, detail) VALUES (?, ?, ?)", (now_iso(), kind, redact(detail)[:300]))
        self._run("DELETE FROM activity WHERE id <= (SELECT MAX(id) FROM activity) - ?", (MAX_ACTIVITY,))

    def activity(self, limit: int = 60) -> list[dict]:
        rows = self._run("SELECT ts, kind, detail FROM activity ORDER BY id DESC LIMIT ?", (limit,),
                         fetch=True, default=[])
        return [{"ts": r[0], "kind": r[1], "detail": r[2]} for r in rows]

    # ---- diagnostics
    def counts(self) -> dict:
        out = {}
        for table in ("messages", "preferences", "activity"):
            rows = self._run(f"SELECT COUNT(*) FROM {table}", fetch=True, default=[(0,)])
            out[table] = rows[0][0]
        return out

    def integrity(self) -> str:
        try:
            return self._conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as e:
            return f"error: {type(e).__name__}"

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size if self.persistent and self.path else 0
        except OSError:
            return 0

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
