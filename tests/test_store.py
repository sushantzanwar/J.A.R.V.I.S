"""Persistence, migration, recovery and safety behavior of the local store."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis.core.store import MAX_MESSAGES, MAX_TEXT, MIGRATIONS, Store, default_data_dir


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "data" / "jarvis.db"

    def open(self, **kw) -> Store:
        s = Store(self.db, **kw)
        self.addCleanup(s.close)
        return s


class PersistenceTests(StoreCase):
    def test_messages_prefs_and_activity_survive_reopening(self):
        s = self.open()
        self.assertTrue(s.persistent)
        self.assertEqual(s.notice, "")
        s.add_message("user", "hello")
        s.add_message("jarvis", "hi there")
        s.set_pref("provider", "claude-code")
        s.set_pref("save_history", False)
        s.add_activity("provider_changed", "Language model set to claude-code")
        s.close()
        t = self.open()
        self.assertEqual([(m["role"], m["text"]) for m in t.messages()], [("user", "hello"), ("jarvis", "hi there")])
        self.assertEqual(t.get_pref("provider"), "claude-code")
        self.assertIs(t.get_pref("save_history"), False)
        self.assertEqual(t.activity()[0]["kind"], "provider_changed")
        self.assertEqual(t.notice, "")

    def test_creates_the_data_folder_and_defaults(self):
        s = self.open()
        self.assertTrue(self.db.exists())
        self.assertEqual((s.get_pref("provider"), s.get_pref("save_history"), s.get_pref("reduce_motion")),
                         ("offline", True, False))
        self.assertEqual(s.messages(), [])

    def test_messages_are_ordered_and_limited(self):
        s = self.open()
        for i in range(5):
            s.add_message("user", f"m{i}")
        self.assertEqual([m["text"] for m in s.messages(limit=3)], ["m2", "m3", "m4"])

    def test_retention_caps_history(self):
        s = self.open()
        for i in range(MAX_MESSAGES + 25):
            s._conn.execute("INSERT INTO messages (ts, role, text) VALUES ('x','user','filler')")
        s.add_message("user", "newest")
        self.assertEqual(s.counts()["messages"], MAX_MESSAGES)
        self.assertEqual(s.messages(limit=1)[0]["text"], "newest")

    def test_clear_messages_keeps_prefs_and_notes_the_event(self):
        s = self.open()
        s.add_message("user", "one")
        s.set_pref("reduce_motion", True)
        self.assertEqual(s.clear_messages(), 1)
        self.assertEqual(s.messages(), [])
        self.assertIs(s.get_pref("reduce_motion"), True)
        self.assertEqual(s.activity()[0]["kind"], "history_cleared")

    def test_reset_prefs(self):
        s = self.open()
        s.set_pref("provider", "claude-code")
        s.reset_prefs()
        self.assertEqual(s.get_pref("provider"), "offline")


class SafetyTests(StoreCase):
    def test_secrets_are_redacted_before_saving(self):
        s = self.open()
        s.add_message("user", "my key is sk-abcdefghijklmnopqrstuv and password=hunter22hunter")
        s.add_activity("note", "token=abcdef123456 leaked")
        s.close()
        raw = self.db.read_bytes()
        for secret in (b"sk-abcdefghijklmnopqrstuv", b"hunter22hunter", b"abcdef123456"):
            self.assertNotIn(secret, raw)
        self.assertIn("[REDACTED]", self.open().messages()[0]["text"])

    def test_long_text_is_truncated(self):
        s = self.open()
        s.add_message("user", "x" * (MAX_TEXT + 500))
        self.assertEqual(len(s.messages()[0]["text"]), MAX_TEXT)

    def test_only_known_safe_preferences_can_be_set(self):
        s = self.open()
        for key, value in (("provider", "rm -rf"), ("provider", "evil"), ("save_history", "yes"),
                           ("api_key", "sk-123"), ("worker_flags", "--allow-commands"), ("reduce_motion", 1)):
            with self.assertRaises(ValueError, msg=f"{key}={value!r}"):
                s.set_pref(key, value)
        self.assertEqual(s.get_pref("provider"), "offline")

    def test_tampered_preference_value_falls_back_to_default(self):
        s = self.open()
        s._conn.execute("INSERT INTO preferences (key, value) VALUES ('provider', '\"evil\"')")
        s._conn.commit()
        self.assertEqual(s.get_pref("provider"), "offline")
        s._conn.execute("UPDATE preferences SET value = 'not json' WHERE key = 'provider'")
        s._conn.commit()
        self.assertEqual(s.get_pref("provider"), "offline")

    def test_sql_injection_in_text_is_just_text(self):
        s = self.open()
        s.add_message("user", "x'); DROP TABLE messages; --")
        self.assertEqual(s.messages()[0]["text"], "x'); DROP TABLE messages; --")


class RecoveryTests(StoreCase):
    def test_garbage_file_is_preserved_and_replaced_with_notice(self):
        self.db.parent.mkdir(parents=True)
        self.db.write_bytes(b"this is definitely not a sqlite database" * 50)
        s = self.open()
        self.assertTrue(s.persistent)
        self.assertIn("could not be read", s.notice)
        self.assertIsNotNone(s.quarantine_path)
        self.assertTrue(s.quarantine_path.exists())                       # nothing was deleted
        self.assertTrue(s.quarantine_path.read_bytes().startswith(b"this is definitely"))
        self.assertIn(str(s.quarantine_path), s.notice)
        s.add_message("user", "works again")                              # the fresh database is usable
        self.assertEqual(s.messages()[0]["text"], "works again")

    def test_truncated_real_database_is_quarantined(self):
        s = self.open()
        for i in range(200):
            s.add_message("user", "data " * 40)
        s.close()
        size = self.db.stat().st_size
        self.db.write_bytes(self.db.read_bytes()[: size // 3])
        t = self.open()
        self.assertTrue(t.quarantine_path and t.quarantine_path.exists())
        self.assertIn("could not be read", t.notice)

    def test_corrupted_pages_fail_the_integrity_check(self):
        s = self.open()
        for i in range(300):
            s.add_message("user", "payload " * 30)
        s.close()
        data = bytearray(self.db.read_bytes())
        for i in range(4096 * 2, len(data) - 1, 97):                      # scramble most of the content pages
            data[i] ^= 0xFF
        self.db.write_bytes(bytes(data))
        t = self.open()
        self.assertTrue(t.quarantine_path and t.quarantine_path.exists(), t.notice)

    def test_empty_zero_byte_file_is_just_initialised(self):
        self.db.parent.mkdir(parents=True)
        self.db.write_bytes(b"")
        s = self.open()
        self.assertTrue(s.persistent)
        self.assertEqual(s.notice, "")
        self.assertEqual(s.counts()["messages"], 0)

    def test_unavailable_folder_falls_back_to_memory_with_notice(self):
        blocker = self.dir / "blocker"
        blocker.write_text("a file where a folder is needed")
        s = Store(blocker / "sub" / "jarvis.db")
        self.addCleanup(s.close)
        self.assertFalse(s.persistent)
        self.assertIn("unavailable", s.notice)
        s.add_message("user", "still usable this session")
        self.assertEqual(len(s.messages()), 1)
        self.assertFalse((blocker / "sub").exists())

    def test_database_from_a_newer_app_is_left_untouched(self):
        self.db.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE future (x)")
        conn.execute("INSERT INTO future VALUES ('keep me')")
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        before = self.db.read_bytes()
        s = self.open()
        self.assertFalse(s.persistent)
        self.assertIn("newer version", s.notice)
        s.add_message("user", "memory only")
        s.close()
        self.assertEqual(self.db.read_bytes(), before)                    # byte-for-byte unchanged
        self.assertEqual(list(self.db.parent.glob("*.corrupt-*")), [])

    def test_failed_write_is_reported_not_swallowed(self):
        s = self.open()
        s._conn.execute("DROP TABLE messages")
        s._conn.commit()
        s.add_message("user", "nowhere to go")
        self.assertIn("could not be saved", s.notice)
        self.assertEqual(s.messages(), [])
        self.assertIn("could not be read", s.notice)


class MigrationTests(StoreCase):
    V2 = {**MIGRATIONS, 2: ["ALTER TABLE messages ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"]}
    BROKEN = {**MIGRATIONS, 2: ["ALTER TABLE messages ADD COLUMN ok INTEGER", "THIS IS NOT SQL"]}

    def test_old_database_is_migrated_with_a_backup_and_keeps_data(self):
        s = self.open()
        s.add_message("user", "precious")
        s.close()
        m = self.open(migrations=self.V2)
        self.assertEqual(m.notice, "")
        self.assertTrue(m.persistent)
        self.assertEqual(m._conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(m.messages()[0]["text"], "precious")
        self.assertEqual(m._conn.execute("SELECT pinned FROM messages").fetchone()[0], 0)
        self.assertIsNotNone(m.backup_path)
        backup = sqlite3.connect(m.backup_path)
        self.addCleanup(backup.close)
        self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_failed_migration_rolls_back_and_fails_closed(self):
        s = self.open()
        s.add_message("user", "precious")
        s.close()
        m = self.open(migrations=self.BROKEN)
        self.assertFalse(m.persistent)
        self.assertIn("Updating your saved data failed", m.notice)
        self.assertIn("bak-v1", m.notice)
        m.close()
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 1)            # not half-migrated
        self.assertNotIn("ok", [c[1] for c in raw.execute("PRAGMA table_info(messages)")])
        self.assertEqual(raw.execute("SELECT text FROM messages").fetchone()[0], "precious")

    def test_fresh_database_gets_latest_schema_without_backup(self):
        m = self.open(migrations=self.V2)
        self.assertIsNone(m.backup_path)
        self.assertEqual(m._conn.execute("PRAGMA user_version").fetchone()[0], 2)


class LocationTests(unittest.TestCase):
    def test_default_location_is_outside_the_repository(self):
        old = os.environ.pop("JARVIS_DATA_DIR", None)
        try:
            repo = Path(__file__).resolve().parents[1]
            d = default_data_dir()
            self.assertNotIn(repo, d.resolve().parents)
            self.assertEqual(d.name, "Jarvis")
        finally:
            if old is not None:
                os.environ["JARVIS_DATA_DIR"] = old

    def test_override_via_environment(self):
        os.environ["JARVIS_DATA_DIR"] = r"C:\somewhere\else"
        try:
            self.assertEqual(default_data_dir(), Path(r"C:\somewhere\else"))
        finally:
            del os.environ["JARVIS_DATA_DIR"]

    def test_memory_store_has_no_file_and_no_notice(self):
        s = Store(None)
        s.add_message("user", "x")
        self.assertIsNone(s.path)
        self.assertEqual(s.notice, "")
        self.assertFalse(s.persistent)
        s.close()


if __name__ == "__main__":
    unittest.main()
