"""Backend workspace behaviour: history, preferences, provider selection, timeline, notices, safety."""
import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path

from tests.qt_helpers import FakeProvider, gui_app, pump

from jarvis.core.store import Store  # noqa: E402
from jarvis.ui import backend as backend_mod  # noqa: E402
from jarvis.ui.backend import GREETING, Backend  # noqa: E402

JARVIS = Path(backend_mod.__file__).resolve().parents[1]


class WorkspaceCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui_app()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tasks = self.root / "bridge" / "tasks"
        self.tasks.mkdir(parents=True)
        self.db = self.root / "data" / "jarvis.db"
        self._opened = []

    def store(self) -> Store:
        s = Store(self.db)
        self._opened.append(s)
        self.addCleanup(s.close)
        return s

    def backend(self, provider=None, store=None, factory=None) -> Backend:
        provider = provider or FakeProvider(name="offline", state="builtin", detail="built-in responder")
        b = Backend(self.tasks, provider, store=store or self.store(),
                    factory=factory or (lambda name: FakeProvider(name=name)))
        self.assertTrue(pump(lambda: b.providerState != "checking"))
        return b

    def ask(self, b, text):
        n = len(b.messages)
        b.send(text)
        self.assertTrue(pump(lambda: len(b.messages) >= n + 2 and b.orbMode != "thinking"), "no answer")

    def add_task(self, i, **kw):
        t = {"id": i, "title": f"task {i}", "status": "pending", "approved": False, "needs": "read-only",
             "updated": "x", **kw}
        (self.tasks / f"{i:03d}.json").write_text(json.dumps(t), encoding="utf-8")


class HistoryTests(WorkspaceCase):
    def test_history_persists_across_restarts_with_timestamps(self):
        b = self.backend()
        self.assertEqual([m["text"] for m in b.messages], [GREETING])      # greeting only when history is empty
        self.ask(b, "status")
        self.assertEqual(len(b.messages), 3)
        self.assertTrue(all(m["time"] for m in b.messages))
        self._opened[-1].close()
        b2 = self.backend(store=self.store())
        self.assertEqual([m["role"] for m in b2.messages], ["user", "jarvis"])   # the greeting is not stored
        self.assertEqual(b2.messages[0]["text"], "status")
        self.assertIn("CPU", b2.messages[1]["text"])

    def test_clear_history_resets_view_and_database(self):
        b = self.backend()
        self.ask(b, "help")
        b.clearHistory()
        self.assertEqual([m["text"] for m in b.messages], [GREETING])
        self.assertEqual(self._opened[0].counts()["messages"], 0)
        self.assertIn("Conversation history cleared", [t["text"] for t in b.timeline])

    def test_history_saving_can_be_turned_off(self):
        b = self.backend()
        b.setSaveHistory(False)
        self.assertFalse(b.saveHistory)
        self.ask(b, "help")
        self.assertEqual(len(b.messages), 3)                               # still shown in this session
        self.assertEqual(self._opened[0].counts()["messages"], 0)          # but not written
        b.setSaveHistory(True)
        self.ask(b, "time")
        self.assertEqual(self._opened[0].counts()["messages"], 2)

    def test_secrets_typed_into_chat_are_not_saved(self):
        b = self.backend()
        self.ask(b, "my password=hunter22hunter is secret")
        self.assertNotIn(b"hunter22hunter", self.db.read_bytes())

    def test_busy_notice_is_not_saved(self):
        b = self.backend()
        b._busy = True
        b.send("help")
        self.assertEqual(b.messages[-1]["text"], "Still working on your previous question.")
        self.assertEqual(self._opened[0].counts()["messages"], 1)          # only the user's line
        b._busy = False


class PreferenceTests(WorkspaceCase):
    def test_preferences_persist_and_reset(self):
        b = self.backend()
        b.setReduceMotion(True)
        b.setSaveHistory(False)
        self._opened[-1].close()
        b2 = self.backend(store=self.store())
        self.assertTrue(b2.reduceMotion)
        self.assertFalse(b2.saveHistory)
        b2.resetPreferences()
        self.assertFalse(b2.reduceMotion)
        self.assertTrue(b2.saveHistory)
        self.assertIn("Preferences reset to defaults", [t["text"] for t in b2.timeline])

    def test_provider_choice_is_whitelisted_and_persisted(self):
        made = []
        b = self.backend(factory=lambda name: made.append(name) or FakeProvider(name=name))
        b.setProvider("rm -rf /")
        b.setProvider("")
        b.setProvider("offline")                                           # already selected
        self.assertEqual(made, [])
        self.assertEqual(b.providerName, "offline")
        b.setProvider("claude-code")
        self.assertEqual(made, ["claude-code"])
        self.assertEqual(b.providerName, "claude-code")
        self.assertTrue(pump(lambda: b.providerState == "connected"))
        self.assertEqual(self._opened[0].get_pref("provider"), "claude-code")
        self.assertIn("Language model set to claude-code", [t["text"] for t in b.timeline])

    def test_reset_preferences_returns_to_offline(self):
        b = self.backend()
        b.setProvider("claude-code")
        b.resetPreferences()
        self.assertEqual(b.providerName, "offline")

    def test_late_status_from_a_previous_provider_is_ignored(self):
        b = self.backend()
        b.setProvider("claude-code")
        pump(lambda: b.providerState == "connected")
        b._set_provider("offline", "builtin", "stale answer from the old provider")
        self.assertEqual(b.providerState, "connected")
        self.assertNotIn("stale", b.providerDetail)


class ProviderStatusUiTests(WorkspaceCase):
    def test_disconnected_state_is_explained(self):
        b = self.backend(FakeProvider(name="claude-code", state="disconnected",
                                      detail="Claude Code is not signed in (run: claude auth login)"))
        self.assertEqual(b.providerState, "disconnected")
        self.assertIn("not signed in", b.providerExplanation)
        self.assertIn("Re-check", b.providerExplanation)
        self.ask(b, "tell me a joke")
        self.assertIn("unavailable", b.messages[-1]["text"])

    def test_error_state_after_failed_request_is_explained(self):
        b = self.backend(FakeProvider(name="claude-code", exc="usage limit reached"))
        self.ask(b, "tell me a joke")
        self.assertTrue(pump(lambda: b.providerState == "error"))
        self.assertIn("usage limit reached", b.providerExplanation)

    def test_offline_explanation_mentions_what_works(self):
        b = self.backend()
        self.assertIn("built-in commands", b.providerExplanation)


class RecoveryNoticeTests(WorkspaceCase):
    def test_corrupt_data_surfaces_a_notice_that_can_be_dismissed(self):
        self.db.parent.mkdir(parents=True)
        self.db.write_bytes(b"garbage" * 300)
        b = self.backend(store=self.store())
        self.assertIn("could not be read", b.storeNotice)
        self.assertTrue(b.storePersistent)
        self.assertTrue(list(self.db.parent.glob("*.corrupt-*")))
        b.dismissNotice()
        self.assertEqual(b.storeNotice, "")

    def test_unwritable_location_says_nothing_is_saved(self):
        blocker = self.root / "blocker"
        blocker.write_text("x")
        s = Store(blocker / "sub" / "jarvis.db")
        self.addCleanup(s.close)
        b = self.backend(store=s)
        self.assertFalse(b.storePersistent)
        self.assertIn("will not be saved", b.storeNotice)
        self.ask(b, "help")                                                # the app keeps working
        self.assertEqual(len(b.messages), 3)

    def test_runtime_save_failure_raises_a_notice(self):
        s = self.store()
        b = self.backend(store=s)
        s._conn.execute("DROP TABLE messages")
        s._conn.commit()
        self.ask(b, "help")
        self.assertIn("could not be saved", b.storeNotice)
        self.assertEqual(len(b.messages), 3)                               # nothing is lost from the screen


class TimelineAndTaskTests(WorkspaceCase):
    def test_start_is_recorded_and_task_transitions_are_logged_after_a_baseline(self):
        self.add_task(1, status="done", approved=True)
        b = self.backend()
        self.assertEqual([t["text"] for t in b.timeline], ["Jarvis started"])      # existing tasks are not replayed
        self.add_task(2)
        b.refreshTasks()
        self.assertEqual(b.timeline[0]["text"], "Task #2 added: Awaiting approval")
        self.add_task(2, approved=True)
        b.refreshTasks()
        self.assertEqual(b.timeline[0]["text"], "Task #2: Approved, queued")
        self.add_task(2, status="in-progress", approved=True, claimed_by="worker")
        b.refreshTasks()
        self.assertEqual(b.timeline[0]["text"], "Task #2: In progress (background worker)")
        n = len(b.timeline)
        b.refreshTasks()                                                        # no change, no new entry
        self.assertEqual(len(b.timeline), n)

    def test_task_labels_and_counts_reach_the_ui(self):
        self.add_task(1)
        self.add_task(2, approved=True)
        self.add_task(3, status="done", approved=True)
        b = self.backend()
        self.assertEqual(sorted(t["label"] for t in b.tasks), ["Approved, queued", "Awaiting approval", "Done"])
        self.assertEqual(dict(b.taskCounts), {"awaiting": 1, "queued": 1, "running": 0, "blocked": 0, "done": 1})
        self.assertEqual(b.awaitingCount, 1)

    def test_timeline_survives_restart(self):
        b = self.backend()
        b.setProvider("claude-code")
        self._opened[-1].close()
        b2 = self.backend(store=self.store())
        texts = [t["text"] for t in b2.timeline]
        self.assertIn("Language model set to claude-code", texts)
        self.assertEqual(texts.count("Jarvis started"), 2)

    def test_worker_state_is_visible_and_read_only(self):
        b = self.backend()
        self.assertEqual(b.workerState["state"], "unknown")
        (self.tasks.parent / "PAUSE").write_text("x")
        b.refreshWorker()
        self.assertEqual(b.workerState["state"], "paused")
        self.assertTrue((self.tasks.parent / "PAUSE").exists())             # looking changes nothing


class DiagnosticsAndSafetyTests(WorkspaceCase):
    def test_diagnostics_summarise_state_without_content(self):
        self.add_task(1, title="CONFIDENTIAL-TASK-TITLE-XYZ", body="CONFIDENTIAL-BODY-XYZ")
        b = self.backend()
        self.ask(b, "my plan is PLAN-XYZZY")
        text = b.diagnosticsText()
        self.assertIn("persistent: yes", text)
        self.assertIn("messages: 2", text)
        for private in ("PLAN-XYZZY", "CONFIDENTIAL"):
            self.assertNotIn(private, text)

    def test_backend_exposes_no_approval_or_execution_surface(self):
        public = [n for n, _ in inspect.getmembers(Backend) if not n.startswith("_")]
        read_only_worker = {"workerState", "refreshWorker"}               # visibility only: no start/stop/pause controls
        for name in public:
            self.assertNotRegex(name.lower(), r"approve|exec|shell|command|subprocess|^run|allow", f"Backend.{name}")
            if "worker" in name.lower():
                self.assertIn(name, read_only_worker)
        src = inspect.getsource(backend_mod)
        self.assertNotRegex(src, r"\bsubprocess\b|os\.system|admin\.py\s+approve|--allow-")

    def test_ui_code_never_spawns_processes_or_approves(self):
        for p in list((JARVIS / "ui").rglob("*.py")) + list((JARVIS / "ui" / "qml").glob("*.qml")):
            text = p.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\bsubprocess\b|os\.system|Qt\.openUrlExternally|XMLHttpRequest|WebView|"
                                        r"WebSocket|\bProcess\b", text), p.name)
            code = re.sub(r'"[^"\n]*"', '""', text)                       # ignore string literals (help text)
            self.assertNotRegex(code, r"backend\.(approve|run|exec)", p.name)


if __name__ == "__main__":
    unittest.main()
