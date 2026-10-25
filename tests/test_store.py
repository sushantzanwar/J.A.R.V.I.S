"""Persistence, migration, recovery and safety behavior of the local store."""
import json
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
    V1 = {1: MIGRATIONS[1]}
    V2 = {1: MIGRATIONS[1], 2: MIGRATIONS[2]}
    # "the next version after the latest real one", whatever that is
    V4 = {**MIGRATIONS, max(MIGRATIONS) + 1: ["ALTER TABLE messages ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"]}
    BROKEN = {**MIGRATIONS, max(MIGRATIONS) + 1: ["ALTER TABLE messages ADD COLUMN ok INTEGER", "THIS IS NOT SQL"]}

    def test_v1_database_migrates_to_latest_with_backup(self):
        s = self.open(migrations=self.V1)
        s.add_message("user", "v1 message")
        s.close()
        # Open with real latest migrations (v3)
        m = self.open()
        self.assertEqual(m.notice, "")
        self.assertTrue(m.persistent)
        self.assertEqual(m._conn.execute("PRAGMA user_version").fetchone()[0], max(MIGRATIONS))
        self.assertEqual(m.messages()[0]["text"], "v1 message")
        # Memories table is ready and functional
        m.remember("name", "Tony", category="user_fact")
        self.assertEqual(m.recall(key="name")[0]["value"], "Tony")
        self.assertIsNotNone(m.backup_path)
        backup = sqlite3.connect(m.backup_path)
        self.addCleanup(backup.close)
        self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_v2_database_migrates_to_v3_with_backup_and_fts_populated(self):
        s = self.open(migrations=self.V2)
        s.add_message("user", "v2 legacy chat message")
        s.remember("editor", "neovim", category="preference")
        s.close()

        m = self.open()
        self.assertEqual(m.notice, "")
        self.assertTrue(m.persistent)
        self.assertEqual(m._conn.execute("PRAGMA user_version").fetchone()[0], max(MIGRATIONS))
        self.assertEqual(m.messages()[0]["text"], "v2 legacy chat message")
        # Verify FTS tables were populated during migration
        mems = m.search_memories("neovim")
        self.assertTrue(len(mems) > 0)
        self.assertEqual(mems[0]["value"], "neovim")
        msgs = m.search_messages("legacy")
        self.assertTrue(len(msgs) > 0)
        self.assertIn("v2 legacy", msgs[0]["text"])
        self.assertIsNotNone(m.backup_path)
        backup = sqlite3.connect(m.backup_path)
        self.addCleanup(backup.close)
        self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_old_database_is_migrated_with_a_backup_and_keeps_data(self):
        s = self.open()
        s.add_message("user", "precious")
        s.close()
        m = self.open(migrations=self.V4)
        self.assertEqual(m.notice, "")
        self.assertTrue(m.persistent)
        self.assertEqual(m._conn.execute("PRAGMA user_version").fetchone()[0], max(MIGRATIONS) + 1)
        self.assertEqual(m.messages()[0]["text"], "precious")
        self.assertEqual(m._conn.execute("SELECT pinned FROM messages").fetchone()[0], 0)
        self.assertIsNotNone(m.backup_path)
        backup = sqlite3.connect(m.backup_path)
        self.addCleanup(backup.close)
        self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], max(MIGRATIONS))

    def test_failed_migration_rolls_back_and_fails_closed(self):
        s = self.open()
        s.add_message("user", "precious")
        s.close()
        m = self.open(migrations=self.BROKEN)
        self.assertFalse(m.persistent)
        self.assertIn("Updating your saved data failed", m.notice)
        self.assertIn(f"bak-v{max(MIGRATIONS)}", m.notice)
        m.close()
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], max(MIGRATIONS))            # not half-migrated
        self.assertNotIn("ok", [c[1] for c in raw.execute("PRAGMA table_info(messages)")])
        self.assertEqual(raw.execute("SELECT text FROM messages").fetchone()[0], "precious")

    def test_fresh_database_gets_latest_schema_without_backup(self):
        m = self.open(migrations=self.V4)
        self.assertIsNone(m.backup_path)
        self.assertEqual(m._conn.execute("PRAGMA user_version").fetchone()[0], max(MIGRATIONS) + 1)


class MemoryTests(StoreCase):
    def test_remember_and_recall_basic(self):
        s = self.open()
        mem = s.remember("user_name", "Alice", category="user_fact")
        self.assertEqual(mem["key"], "user_name")
        self.assertEqual(mem["value"], "Alice")
        self.assertEqual(mem["category"], "identity")  # normalized alias

        recalled = s.recall(key="user_name", category="identity")
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["value"], "Alice")
        self.assertEqual(recalled[0]["approval_status"], "user_approved")
        self.assertEqual(recalled[0]["confidence"], 1.0)
        self.assertEqual(s.counts()["memories"], 1)

    def test_remember_updates_on_conflict_and_records_history(self):
        s = self.open()
        s.remember("editor", "vim", category="preference")
        s.remember("editor", "vscode", category="preference")
        recalled = s.recall(key="editor", category="preference")
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["value"], "vscode")
        self.assertEqual(s.counts()["memories"], 1)

        # History preserves provenance of previous value
        hist = s.memory_history(key="editor", category="preference")
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["old_value"], "vim")
        self.assertEqual(hist[0]["new_value"], "vscode")
        self.assertEqual(hist[0]["reason"], "updated")
        self.assertIsNone(hist[1]["old_value"])
        self.assertEqual(hist[1]["new_value"], "vim")
        self.assertEqual(hist[1]["reason"], "created")

    def test_recall_filtering(self):
        s = self.open()
        s.remember("theme", "dark", category="preference")
        s.remember("font", "cascadia", category="preference")
        s.remember("name", "Bob", category="identity")

        all_mems = s.recall()
        self.assertEqual(len(all_mems), 3)

        prefs = s.recall(category="preference")
        self.assertEqual(len(prefs), 2)
        self.assertEqual([p["key"] for p in prefs], ["font", "theme"])

        facts = s.recall(category="identity")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["key"], "name")

    def test_archive_memory(self):
        s = self.open()
        s.remember("temp_project", "alpha", category="project")
        self.assertTrue(s.archive("temp_project", category="project"))
        # Hidden from active recall
        self.assertEqual(s.recall(key="temp_project", status="active"), [])
        # Visible when querying archived
        archived = s.recall(key="temp_project", status="archived")
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["value"], "alpha")

        hist = s.memory_history(key="temp_project", category="project")
        self.assertEqual(hist[0]["reason"], "archived")

    def test_exact_category_forget(self):
        s = self.open()
        s.remember("server", "prod-1", category="device")
        s.remember("server", "discord-community", category="contact")

        # Forget only in contact category
        self.assertTrue(s.forget("server", category="contact"))
        self.assertEqual(s.recall(key="server", category="contact"), [])
        # Device server is unharmed
        device_mem = s.recall(key="server", category="device")
        self.assertEqual(len(device_mem), 1)
        self.assertEqual(device_mem[0]["value"], "prod-1")

    def test_forget_single_memory(self):
        s = self.open()
        s.remember("temp_note", "1234")
        self.assertTrue(s.forget("temp_note"))
        self.assertEqual(s.recall(key="temp_note"), [])
        # Forgetting again returns False
        self.assertFalse(s.forget("temp_note"))
        # Deletion is recorded in history
        hist = s.memory_history(key="temp_note")
        self.assertEqual(hist[0]["reason"], "deleted")
        self.assertEqual(hist[0]["old_value"], "1234")

    def test_clear_memories_creates_backup_and_preserves_history(self):
        s = self.open()
        s.remember("p1", "v1", category="preference")
        s.remember("f1", "v2", category="identity")
        s.remember("f2", "v3", category="identity")

        # Clear just identity category
        cleared_facts = s.clear_memories(category="identity", backup=True)
        self.assertEqual(cleared_facts, 2)
        self.assertEqual(len(s.recall()), 1)
        self.assertEqual(s.recall()[0]["key"], "p1")

        # Clear all remaining
        cleared_all = s.clear_memories(backup=True)
        self.assertEqual(cleared_all, 1)
        self.assertEqual(s.recall(), [])

        # Backup snapshot was created
        backups = list(s.path.parent.glob("*.memories-bak-*"))
        self.assertGreaterEqual(len(backups), 1)

    def test_export_memories_json(self):
        s = self.open()
        s.remember("name", "Jarvis User", category="identity")
        json_export = s.export_memories()
        data = json.loads(json_export)
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["memories"]), 1)
        self.assertEqual(data["memories"][0]["key"], "name")
        self.assertEqual(len(data["history"]), 1)

    def test_memory_secrets_are_redacted(self):
        s = self.open()
        s.remember("api_key", "my secret is sk-1234567890abcdef12345678 and token=secrettoken123")
        recalled = s.recall(key="api_key")[0]["value"]
        self.assertNotIn("sk-1234567890abcdef12345678", recalled)
        self.assertIn("[REDACTED]", recalled)

    def test_memory_length_limits(self):
        s = self.open()
        long_val = "z" * (MAX_TEXT + 500)
        s.remember("long_key", long_val)
        val = s.recall(key="long_key")[0]["value"]
        self.assertEqual(len(val), MAX_TEXT)


class FtsAndSummaryTests(StoreCase):
    def test_sanitize_fts_query(self):
        from jarvis.core.store import sanitize_fts_query
        self.assertEqual(sanitize_fts_query(""), "")
        self.assertEqual(sanitize_fts_query("   "), "")
        self.assertEqual(sanitize_fts_query("???***"), "")
        self.assertEqual(sanitize_fts_query("what is my name?"), "name*")
        # Earlier words exact, last word gets prefix wildcard if >=3 chars
        q = sanitize_fts_query("Tony Stark")
        self.assertIn('"tony"', q)
        self.assertIn("stark*", q)

    def test_search_memories_bm25_ordering(self):
        s = self.open()
        s.remember("editor", "vscode text editor", category="preference")
        s.remember("name", "Tony Stark", category="identity")
        s.remember("notes", "vscode python settings and editor configuration", category="general")

        res = s.search_memories("vscode editor")
        self.assertTrue(len(res) >= 2)
        # Record with both terms ranks highest
        self.assertIn("vscode", res[0]["value"])
        # Observability metrics updated
        self.assertGreaterEqual(s.last_search_latency_ms, 0.0)
        self.assertEqual(s.last_search_results_count, len(res))

    def test_search_memories_category_filter(self):
        s = self.open()
        s.remember("fav_tool", "vscode", category="preference")
        s.remember("project_tool", "vscode", category="project")

        pref_only = s.search_memories("vscode", category="preference")
        self.assertEqual(len(pref_only), 1)
        self.assertEqual(pref_only[0]["category"], "preference")

    def test_search_messages_fts(self):
        s = self.open()
        s.add_message("user", "deploying quantum reactor")
        s.add_message("jarvis", "reactor diagnostics nominal")
        s.add_message("user", "play jazz music")

        res = s.search_messages("reactor")
        self.assertEqual(len(res), 2)
        self.assertTrue(all("reactor" in r["text"] for r in res))

    def test_memory_sync_on_archive_forget_clear(self):
        s = self.open()
        s.remember("city", "Malibu", category="identity")
        self.assertEqual(len(s.search_memories("Malibu")), 1)

        # Archive removes from active FTS
        s.archive("city")
        self.assertEqual(len(s.search_memories("Malibu")), 0)

        # Re-remember adds back
        s.remember("city", "New York", category="identity")
        self.assertEqual(len(s.search_memories("York")), 1)

        # Forget removes from FTS
        s.forget("city")
        self.assertEqual(len(s.search_memories("York")), 0)

        # Clear memories removes all from FTS
        s.remember("hero", "Iron Man", category="identity")
        s.remember("suit", "Mark 85", category="device")
        self.assertEqual(len(s.search_memories("Iron")), 1)
        s.clear_memories()
        self.assertEqual(len(s.search_memories("Iron")), 0)
        self.assertEqual(len(s.search_memories("Mark")), 0)

    def test_retention_pruning_removes_from_messages_fts(self):
        s = self.open()
        s.add_message("user", "ancient message that will be pruned")
        first_id = s.search_messages("ancient")[0]["id"]
        # Fill beyond retention cap
        for i in range(MAX_MESSAGES + 5):
            s._conn.execute("INSERT INTO messages (ts, role, text) VALUES ('x','user','filler')")
            s._conn.execute("INSERT INTO messages_fts (message_id, role, text) VALUES (?, 'user', 'filler')", (10000 + i,))
        s._conn.commit()
        # Adding a new message triggers retention cleanup
        s.add_message("user", "fresh trigger message")

        # Verify ancient message was pruned from base table and from FTS
        self.assertEqual(s._conn.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (first_id,)).fetchone()[0], 0)
        self.assertEqual(len(s.search_messages("ancient")), 0)

    def test_rebuild_fts_and_desync_recovery(self):
        s = self.open()
        s.remember("fruit", "banana", category="preference")
        s.add_message("user", "eating banana")

        # Manually desync FTS tables
        s._conn.execute("DELETE FROM memories_fts")
        s._conn.execute("DELETE FROM messages_fts")
        s._conn.commit()

        self.assertEqual(len(s.search_memories("banana")), 0)
        self.assertEqual(len(s.search_messages("banana")), 0)

        res = s.rebuild_fts()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["memories_indexed"], 1)
        self.assertEqual(res["messages_indexed"], 1)
        self.assertIsNotNone(res["rebuilt_at"])

        self.assertEqual(len(s.search_memories("banana")), 1)
        self.assertEqual(len(s.search_messages("banana")), 1)

    def test_conversation_summaries_storage_and_delete(self):
        s = self.open()
        s.add_message("user", "turn 1")
        s.add_message("jarvis", "turn 2")
        sum1 = s.add_summary(1, 2, "First batch summary", ["point 1", "point 2"])
        self.assertIsNotNone(sum1["id"])
        self.assertEqual(sum1["summary"], "First batch summary")
        self.assertTrue(sum1["is_derived"])

        sums = s.summaries(limit=5)
        self.assertEqual(len(sums), 1)
        self.assertEqual(sums[0]["key_points"], ["point 1", "point 2"])
        self.assertEqual(s.counts()["conversation_summaries"], 1)

        # Deleting summary deletes summary record without touching messages
        deleted = s.delete_summary(sum1["id"])
        self.assertTrue(deleted)
        self.assertEqual(len(s.summaries()), 0)
        self.assertEqual(len(s.messages()), 2)  # raw history preserved!


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
