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
import re
import shutil
import sqlite3
import time
from pathlib import Path

from jarvis.core.providers import PROVIDER_NAMES
from jarvis.core.redact import redact

MAX_TEXT = 4000
MAX_MESSAGES = 1000
MAX_ACTIVITY = 500
MAX_MEMORY_KEY = 100
MAX_MEMORY_CATEGORY = 50

ALLOWED_CATEGORIES = (
    "identity",
    "preference",
    "goal",
    "project",
    "contact",
    "device",
    "general",
)

CATEGORY_ALIASES = {
    "user_fact": "identity",
    "fact": "identity",
    "name": "identity",
    "prefs": "preference",
    "settings": "preference",
    "task": "goal",
    "system": "device",
    "hardware": "device",
}


def normalize_key(k: str) -> str:
    """Normalize a memory key to clean snake_case: lowercase, stripped, collapsed spaces/hyphens."""
    k = k.strip().lower()
    k = re.sub(r"[\s\-]+", "_", k)
    k = re.sub(r"[^a-z0-9_]", "", k)
    return k[:MAX_MEMORY_KEY] or "note"


def normalize_category(c: str) -> str:
    """Map category alias or default to allowed categories."""
    c = c.strip().lower().replace(" ", "_")
    c = CATEGORY_ALIASES.get(c, c)
    return c if c in ALLOWED_CATEGORIES else "general"


COMMON_STOPWORDS = {
    "what", "is", "are", "my", "your", "the", "a", "an", "and", "or", "in", "on", "at",
    "to", "for", "of", "with", "do", "you", "me", "tell", "show", "how", "can", "please"
}


def sanitize_fts_query(query: str, operator: str = "OR", custom_stopwords: set | None = None) -> str:
    """Sanitize user input into a safe FTS5 query with tightened wildcard policy.

    Punctuation and operators that could cause FTS syntax errors are filtered.
    Meaningful keywords are extracted.
    Tightened wildcard policy:
      * Only the LAST token gets a wildcard prefix (e.g. 'editor vscod*').
      * Earlier tokens are matched as exact words (e.g. '"editor"').
      * Wildcards on the last token require at least 3 characters.
    """
    if not query or not query.strip():
        return ""
    stopwords = custom_stopwords if custom_stopwords is not None else COMMON_STOPWORDS
    tokens = [t.lower() for t in re.findall(r"\b[a-zA-Z0-9_]+\b", query) if len(t) >= 2]
    if not tokens:
        tokens = [t.lower() for t in re.findall(r"\b[a-zA-Z0-9_]+\b", query) if t]
    if not tokens:
        return ""
    meaningful = [t for t in tokens if t not in stopwords]
    active_tokens = meaningful if meaningful else tokens

    formatted_terms = []
    for i, t in enumerate(active_tokens):
        is_last = (i == len(active_tokens) - 1)
        if is_last and len(t) >= 3:
            formatted_terms.append(f"{t}*")
        else:
            formatted_terms.append(f'"{t}"')

    op = f" {operator.strip().upper()} "
    return op.join(formatted_terms)


# version -> statements that bring the schema to that version
MIGRATIONS = {
    1: [
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL)",
        "CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)",
    ],
    2: [
        "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general', key TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'user', confidence REAL NOT NULL DEFAULT 1.0, approval_status TEXT NOT NULL DEFAULT 'user_approved', status TEXT NOT NULL DEFAULT 'active', source_message_id INTEGER, valid_from TEXT, valid_until TEXT, updated_at TEXT NOT NULL, UNIQUE(category, key))",
        "CREATE INDEX idx_memories_cat_status ON memories (category, status)",
        "CREATE INDEX idx_memories_key ON memories (key)",
        "CREATE TABLE memory_history (id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER, ts TEXT NOT NULL, category TEXT NOT NULL, key TEXT NOT NULL, old_value TEXT, new_value TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL)",
        "CREATE INDEX idx_mem_hist_key ON memory_history (category, key)",
    ],
    3: [
        "CREATE VIRTUAL TABLE memories_fts USING fts5(memory_id UNINDEXED, category, key, value)",
        "CREATE VIRTUAL TABLE messages_fts USING fts5(message_id UNINDEXED, role, text)",
        "CREATE TABLE conversation_summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, start_message_id INTEGER NOT NULL, end_message_id INTEGER NOT NULL, summary TEXT NOT NULL, key_points TEXT NOT NULL DEFAULT '[]')",
        "CREATE INDEX idx_summaries_ts ON conversation_summaries (ts)",
        "INSERT INTO memories_fts (memory_id, category, key, value) SELECT id, category, key, value FROM memories WHERE status = 'active'",
        "INSERT INTO messages_fts (message_id, role, text) SELECT id, role, text FROM messages",
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
        self.last_rebuild_ts: str | None = None
        self.last_search_latency_ms: float = 0.0
        self.last_search_results_count: int = 0
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
        msg_id = None
        try:
            with self._conn:
                cur = self._conn.execute("INSERT INTO messages (ts, role, text) VALUES (?, ?, ?)", (ts, role, text))
                msg_id = cur.lastrowid
                try:
                    self._conn.execute("INSERT INTO messages_fts (message_id, role, text) VALUES (?, ?, ?)",
                                       (msg_id, role, text))
                except sqlite3.OperationalError:
                    pass
                self._conn.execute("DELETE FROM messages WHERE id <= (SELECT MAX(id) FROM messages) - ?", (MAX_MESSAGES,))
                try:
                    self._conn.execute("DELETE FROM messages_fts WHERE message_id <= (SELECT MAX(id) FROM messages) - ?", (MAX_MESSAGES,))
                except sqlite3.OperationalError:
                    pass
        except sqlite3.Error as e:
            self.notice = f"Local data could not be saved ({type(e).__name__})."
        return {"id": msg_id, "ts": ts, "role": role, "text": text}

    def messages(self, limit: int = 200) -> list[dict]:
        rows = self._run("SELECT ts, role, text FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id",
                         (limit,), fetch=True, default=[])
        return [{"ts": r[0], "role": r[1], "text": r[2]} for r in rows]

    def clear_messages(self) -> int:
        n = self._run("DELETE FROM messages") or 0
        try:
            self._run("DELETE FROM messages_fts")
        except Exception:
            pass
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

    # ---- durable memories (structured user facts and preferences)
    def remember(self, key: str, value: str, category: str = "general", source: str = "user",
                 confidence: float = 1.0, approval_status: str = "user_approved",
                 source_message_id: int | None = None, valid_from: str | None = None,
                 valid_until: str | None = None) -> dict:
        norm_key = normalize_key(key)
        norm_cat = normalize_category(category)
        val = redact(value.strip())[:MAX_TEXT]
        source = source.strip().lower()[:30] or "user"
        now = now_iso()

        # Check existing row to track previous value in history
        rows = self._run("SELECT id, value, status FROM memories WHERE category = ? AND key = ?",
                         (norm_cat, norm_key), fetch=True, default=[])
        old_id = rows[0][0] if rows else None
        old_val = rows[0][1] if rows else None

        self._run(
            "INSERT INTO memories (ts, category, key, value, source, confidence, approval_status, status, "
            "source_message_id, valid_from, valid_until, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?) "
            "ON CONFLICT(category, key) DO UPDATE SET "
            "value = excluded.value, source = excluded.source, confidence = excluded.confidence, "
            "approval_status = excluded.approval_status, status = 'active', "
            "source_message_id = COALESCE(excluded.source_message_id, memories.source_message_id), "
            "valid_from = excluded.valid_from, valid_until = excluded.valid_until, updated_at = excluded.updated_at",
            (now, norm_cat, norm_key, val, source, confidence, approval_status, source_message_id,
             valid_from, valid_until, now)
        )

        id_row = self._run("SELECT id FROM memories WHERE category = ? AND key = ?",
                           (norm_cat, norm_key), fetch=True, default=[])
        mem_id = id_row[0][0] if id_row else old_id

        if old_val is not None:
            reason = "updated" if old_val != val else "reactivated"
            self._run("INSERT INTO memory_history (memory_id, ts, category, key, old_value, new_value, source, reason) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (mem_id, now, norm_cat, norm_key, old_val, val, source, reason))
            self.add_activity("memory_updated", f"Updated [{norm_cat}] {norm_key}: {val[:50]}")
        else:
            self._run("INSERT INTO memory_history (memory_id, ts, category, key, old_value, new_value, source, reason) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (mem_id, now, norm_cat, norm_key, None, val, source, "created"))
            self.add_activity("memory_saved", f"Remembered [{norm_cat}] {norm_key}: {val[:50]}")

        # Keep memories_fts synchronized with active memories
        try:
            self._run("DELETE FROM memories_fts WHERE memory_id = ?", (mem_id,))
            self._run("INSERT INTO memories_fts (memory_id, category, key, value) VALUES (?, ?, ?, ?)",
                      (mem_id, norm_cat, norm_key, val))
        except sqlite3.OperationalError:
            pass

        return {"id": mem_id, "category": norm_cat, "key": norm_key, "value": val, "source": source,
                "confidence": confidence, "approval_status": approval_status, "status": "active",
                "updated_at": now}

    def recall(self, key: str | None = None, category: str | None = None, status: str | None = "active",
               limit: int = 50, log_usage: bool = False, max_chars: int = 4000) -> list[dict]:
        norm_key = normalize_key(key) if key else None
        norm_cat = normalize_category(category) if category else None

        sql_parts = ["SELECT id, ts, category, key, value, source, confidence, approval_status, status, "
                     "source_message_id, valid_from, valid_until, updated_at FROM memories WHERE 1=1"]
        args = []
        if status:
            sql_parts.append("AND status = ?")
            args.append(status)
        if norm_cat:
            sql_parts.append("AND category = ?")
            args.append(norm_cat)
        if norm_key:
            sql_parts.append("AND key = ?")
            args.append(norm_key)
        sql_parts.append("ORDER BY updated_at DESC, id DESC LIMIT ?")
        args.append(max(1, limit))

        rows = self._run(" ".join(sql_parts), tuple(args), fetch=True, default=[])
        if log_usage and rows:
            self.add_activity("memory_recalled", f"Recalled {len(rows)} memory record(s)")

        return [{
            "id": r[0], "ts": r[1], "category": r[2], "key": r[3],
            "value": r[4][:max_chars], "source": r[5], "confidence": r[6],
            "approval_status": r[7], "status": r[8], "source_message_id": r[9],
            "valid_from": r[10], "valid_until": r[11], "updated_at": r[12]
        } for r in rows]

    def archive(self, key: str, category: str | None = None) -> bool:
        norm_key = normalize_key(key)
        norm_cat = normalize_category(category) if category else None
        if norm_cat:
            rows = self._run("SELECT id, value FROM memories WHERE category = ? AND key = ? AND status != 'archived'",
                             (norm_cat, norm_key), fetch=True, default=[])
        else:
            rows = self._run("SELECT id, category, value FROM memories WHERE key = ? AND status != 'archived'",
                             (norm_key,), fetch=True, default=[])
        if not rows:
            return False

        now = now_iso()
        for r in rows:
            m_id = r[0]
            cat = norm_cat or r[1]
            val = r[1] if norm_cat else r[2]
            self._run("UPDATE memories SET status = 'archived', updated_at = ? WHERE id = ?", (now, m_id))
            try:
                self._run("DELETE FROM memories_fts WHERE memory_id = ?", (m_id,))
            except sqlite3.OperationalError:
                pass
            self._run("INSERT INTO memory_history (memory_id, ts, category, key, old_value, new_value, source, reason) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (m_id, now, cat, norm_key, val, val, "user", "archived"))
            self.add_activity("memory_archived", f"Archived [{cat}] {norm_key}")
        return True

    def forget(self, key: str, category: str | None = None) -> bool:
        norm_key = normalize_key(key)
        norm_cat = normalize_category(category) if category else None
        if norm_cat:
            rows = self._run("SELECT id, value FROM memories WHERE category = ? AND key = ?",
                             (norm_cat, norm_key), fetch=True, default=[])
        else:
            rows = self._run("SELECT id, category, value FROM memories WHERE key = ?",
                             (norm_key,), fetch=True, default=[])
        if not rows:
            return False

        now = now_iso()
        for r in rows:
            m_id = r[0]
            cat = norm_cat or r[1]
            val = r[1] if norm_cat else r[2]
            self._run("INSERT INTO memory_history (memory_id, ts, category, key, old_value, new_value, source, reason) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (m_id, now, cat, norm_key, val, "", "user", "deleted"))
            self._run("DELETE FROM memories WHERE id = ?", (m_id,))
            try:
                self._run("DELETE FROM memories_fts WHERE memory_id = ?", (m_id,))
            except sqlite3.OperationalError:
                pass
            self.add_activity("memory_forgotten", f"Deleted memory [{cat}] {norm_key}")
        return True

    def memory_history(self, key: str | None = None, category: str | None = None, limit: int = 50) -> list[dict]:
        sql_parts = ["SELECT id, memory_id, ts, category, key, old_value, new_value, source, reason "
                     "FROM memory_history WHERE 1=1"]
        args = []
        if category:
            sql_parts.append("AND category = ?")
            args.append(normalize_category(category))
        if key:
            sql_parts.append("AND key = ?")
            args.append(normalize_key(key))
        sql_parts.append("ORDER BY id DESC LIMIT ?")
        args.append(max(1, limit))

        rows = self._run(" ".join(sql_parts), tuple(args), fetch=True, default=[])
        return [{
            "id": r[0], "memory_id": r[1], "ts": r[2], "category": r[3],
            "key": r[4], "old_value": r[5], "new_value": r[6], "source": r[7], "reason": r[8]
        } for r in rows]

    def clear_memories(self, category: str | None = None, backup: bool = True) -> int:
        norm_cat = normalize_category(category) if category else None
        if backup and self.persistent and self.path and self.path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = self.path.with_name(f"{self.path.name}.memories-bak-{stamp}")
            try:
                shutil.copy2(self.path, dest)
            except OSError:
                pass

        if norm_cat:
            rows = self._run("SELECT id, key, value FROM memories WHERE category = ?", (norm_cat,), fetch=True, default=[])
        else:
            rows = self._run("SELECT id, category, key, value FROM memories", fetch=True, default=[])

        now = now_iso()
        for r in rows:
            m_id = r[0]
            cat = norm_cat or r[1]
            k = r[1] if norm_cat else r[2]
            v = r[2] if norm_cat else r[3]
            self._run("INSERT INTO memory_history (memory_id, ts, category, key, old_value, new_value, source, reason) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (m_id, now, cat, k, v, "", "user", "cleared"))

        if norm_cat:
            n = self._run("DELETE FROM memories WHERE category = ?", (norm_cat,)) or 0
            try:
                self._run("DELETE FROM memories_fts WHERE category = ?", (norm_cat,))
            except Exception:
                pass
        else:
            n = self._run("DELETE FROM memories") or 0
            try:
                self._run("DELETE FROM memories_fts")
            except Exception:
                pass

        self.add_activity("memories_cleared", f"{n} memory record(s) deleted")
        return n

    def search_memories(self, query: str, limit: int = 10, category: str | None = None) -> list[dict]:
        """Full-text search across active memories using FTS5 and BM25 rank."""
        t0 = time.perf_counter()
        norm_cat = normalize_category(category) if category else None
        fts_query = sanitize_fts_query(query)
        if not fts_query:
            res = self.recall(category=norm_cat, limit=limit)
            self.last_search_latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            self.last_search_results_count = len(res)
            return res

        sql_parts = [
            "SELECT m.id, m.ts, m.category, m.key, m.value, m.source, m.confidence, "
            "m.approval_status, m.status, m.source_message_id, m.valid_from, m.valid_until, "
            "m.updated_at, f.rank "
            "FROM memories_fts f "
            "JOIN memories m ON m.id = f.memory_id "
            "WHERE memories_fts MATCH ? AND m.status = 'active'"
        ]
        args = [fts_query]
        if norm_cat:
            sql_parts.append("AND m.category = ?")
            args.append(norm_cat)
        sql_parts.append("ORDER BY f.rank LIMIT ?")
        args.append(max(1, limit))

        rows = self._run(" ".join(sql_parts), tuple(args), fetch=True, default=[])
        results = [{
            "id": r[0], "ts": r[1], "category": r[2], "key": r[3],
            "value": r[4], "source": r[5], "confidence": r[6],
            "approval_status": r[7], "status": r[8], "source_message_id": r[9],
            "valid_from": r[10], "valid_until": r[11], "updated_at": r[12],
            "score": round(-float(r[13]), 4) if r[13] is not None else 0.0
        } for r in rows]
        self.last_search_latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        self.last_search_results_count = len(results)
        return results

    def search_messages(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across messages history using FTS5 and BM25 rank."""
        t0 = time.perf_counter()
        fts_query = sanitize_fts_query(query)
        if not fts_query:
            self.last_search_latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            self.last_search_results_count = 0
            return []

        sql = (
            "SELECT m.id, m.ts, m.role, m.text, f.rank "
            "FROM messages_fts f "
            "JOIN messages m ON m.id = f.message_id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY f.rank LIMIT ?"
        )
        rows = self._run(sql, (fts_query, max(1, limit)), fetch=True, default=[])
        results = [{
            "id": r[0], "ts": r[1], "role": r[2], "text": r[3],
            "score": round(-float(r[4]), 4) if r[4] is not None else 0.0
        } for r in rows]
        self.last_search_latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        self.last_search_results_count = len(results)
        return results

    def rebuild_fts(self) -> dict:
        """Rebuild both FTS5 virtual tables from authoritative tables."""
        try:
            with self._conn:
                self._conn.execute("DELETE FROM memories_fts")
                self._conn.execute(
                    "INSERT INTO memories_fts (memory_id, category, key, value) "
                    "SELECT id, category, key, value FROM memories WHERE status = 'active'"
                )
                mem_count = self._conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]

                self._conn.execute("DELETE FROM messages_fts")
                self._conn.execute(
                    "INSERT INTO messages_fts (message_id, role, text) "
                    "SELECT id, role, text FROM messages"
                )
                msg_count = self._conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]

            self.last_rebuild_ts = now_iso()
            self.add_activity("fts_rebuilt", f"Rebuilt FTS: {mem_count} memories, {msg_count} messages")
            return {"status": "ok", "memories_indexed": mem_count, "messages_indexed": msg_count,
                    "rebuilt_at": self.last_rebuild_ts}
        except sqlite3.Error as e:
            return {"status": "error", "error": str(e)}

    def add_summary(self, start_message_id: int, end_message_id: int, summary: str,
                    key_points: list[str] | None = None, ts: str | None = None) -> dict:
        ts = ts or now_iso()
        valid_points = [str(p) for p in key_points] if isinstance(key_points, list) else []
        pts_json = json.dumps(valid_points)
        sum_text = redact(summary.strip())[:MAX_TEXT]
        self._run(
            "INSERT INTO conversation_summaries (ts, start_message_id, end_message_id, summary, key_points) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, start_message_id, end_message_id, sum_text, pts_json)
        )
        row = self._run("SELECT MAX(id) FROM conversation_summaries", fetch=True, default=[(None,)])
        sum_id = row[0][0] if row else None
        self.add_activity("summary_created", f"Summary #{sum_id}: {sum_text[:60]}")
        return {
            "id": sum_id, "ts": ts, "start_message_id": start_message_id,
            "end_message_id": end_message_id, "summary": sum_text, "key_points": valid_points,
            "is_derived": True, "notice": "Derived extractive summary; may be incomplete"
        }

    def delete_summary(self, summary_id: int) -> bool:
        """Delete a derived conversation summary without modifying raw message history."""
        n = self._run("DELETE FROM conversation_summaries WHERE id = ?", (summary_id,)) or 0
        if n > 0:
            self.add_activity("summary_deleted", f"Deleted summary #{summary_id}")
            return True
        return False

    def summaries(self, limit: int = 5) -> list[dict]:
        rows = self._run(
            "SELECT id, ts, start_message_id, end_message_id, summary, key_points "
            "FROM conversation_summaries ORDER BY id DESC LIMIT ?",
            (max(1, limit),), fetch=True, default=[]
        )
        out = []
        for r in rows:
            try:
                pts = json.loads(r[5])
                if not isinstance(pts, list):
                    pts = []
            except (ValueError, TypeError):
                pts = []
            out.append({
                "id": r[0], "ts": r[1], "start_message_id": r[2],
                "end_message_id": r[3], "summary": r[4], "key_points": pts,
                "is_derived": True, "notice": "Derived extractive summary; may be incomplete"
            })
        return out

    def export_memories(self, dest_path: Path | str | None = None) -> str:
        data = {
            "version": 1,
            "exported_at": now_iso(),
            "memories": self.recall(status=None, limit=5000),
            "history": self.memory_history(limit=5000),
        }
        json_str = json.dumps(data, indent=2)
        if dest_path:
            p = Path(dest_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json_str, encoding="utf-8")
            self.add_activity("memory_exported", f"Exported {len(data['memories'])} memories to {p.name}")
        return json_str

    # ---- diagnostics
    def counts(self) -> dict:
        out = {}
        tables = ("messages", "preferences", "activity", "memories", "memory_history",
                  "conversation_summaries", "memories_fts", "messages_fts")
        for table in tables:
            rows = self._run(f"SELECT COUNT(*) FROM {table}", fetch=True, default=[(0,)])
            out[table] = rows[0][0]
        out["last_rebuild_ts"] = self.last_rebuild_ts
        out["last_search_latency_ms"] = self.last_search_latency_ms
        out["last_search_results_count"] = self.last_search_results_count
        out["max_messages_retention"] = MAX_MESSAGES
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
