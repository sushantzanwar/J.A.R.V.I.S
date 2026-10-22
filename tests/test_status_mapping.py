"""Task status labels, worker state and diagnostics: what the UI says must match what the files say."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from jarvis.core import diagnostics
from jarvis.core.tasks import awaiting_approval, counts, describe, read_tasks
from jarvis.core.worker_state import STALE_AFTER, worker_state


class DescribeTests(unittest.TestCase):
    def d(self, **kw):
        return describe({"status": "pending", "approved": False, **kw}, now=1000.0)

    def test_every_real_state(self):
        cases = [
            (dict(), ("Awaiting approval", "violet")),
            (dict(approved=True), ("Approved, queued", "cyan")),
            (dict(approved=True, next_attempt_at=1500.0), ("Retry scheduled", "cyan")),
            (dict(approved=True, next_attempt_at=900.0), ("Approved, queued", "cyan")),      # backoff elapsed
            (dict(last_error="boom"), ("Failed, needs re-approval", "violet")),
            (dict(status="in-progress", approved=True, claimed_by="worker"), ("In progress (background worker)", "ice")),
            (dict(status="in-progress", approved=True, claimed_by="interactive"), ("In progress (interactive session)", "ice")),
            (dict(status="in-progress", approved=True), ("In progress", "ice")),
            (dict(status="blocked", approved=True), ("Blocked", "violet")),
            (dict(status="done", approved=True), ("Done", "muted")),
            (dict(status="done", approved=False), ("Done", "muted")),
            (dict(status="weird-state"), ("Weird state", "muted")),
        ]
        for fields, expected in cases:
            self.assertEqual(self.d(**fields), expected, fields)

    def test_unapproved_is_never_labelled_queued(self):
        for fields in (dict(), dict(last_error="x"), dict(next_attempt_at=5000.0)):
            self.assertNotIn("queued", self.d(**fields)[0].lower())
            self.assertNotIn("Retry", self.d(**fields)[0])

    def test_read_tasks_carries_labels_and_stays_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rows = [(1, dict(status="done", approved=True)), (2, dict()), (3, dict(approved=True, needs="file-changes")),
                    (4, dict(status="in-progress", approved=True, claimed_by="worker", attempts=1))]
            for i, kw in rows:
                (d / f"{i:03d}.json").write_text(json.dumps({"id": i, "title": f"t{i}", "status": "pending",
                                                              "updated": "x", "needs": "read-only", **kw}))
            before = {p.name: p.read_bytes() for p in d.iterdir()}
            tasks = read_tasks(d)
            self.assertEqual([t["id"] for t in tasks], [4, 3, 2, 1])
            self.assertEqual([t["label"] for t in tasks],
                             ["In progress (background worker)", "Approved, queued", "Awaiting approval", "Done"])
            self.assertEqual(tasks[0]["attempts"], 1)
            self.assertEqual(counts(tasks), {"awaiting": 1, "queued": 1, "running": 1, "blocked": 0, "done": 1})
            self.assertEqual([t["id"] for t in awaiting_approval(tasks)], [2])
            self.assertEqual(before, {p.name: p.read_bytes() for p in d.iterdir()})


class WorkerStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = Path(self._tmp.name) / "bridge"
        self.bridge.mkdir()
        self.log = Path(self._tmp.name) / "logs" / "worker.jsonl"
        self.log.parent.mkdir()
        self.now = datetime(2026, 10, 22, 15, 0, 0)

    def write_log(self, *events):
        self.log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    def at(self, minutes_ago):
        return (self.now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")

    def state(self):
        return worker_state(self.bridge, self.log, now=self.now)

    def test_no_log_is_unknown_not_active(self):
        s = self.state()
        self.assertEqual(s["state"], "unknown")
        self.assertIn("may not be installed", s["detail"])

    def test_recent_pass_is_active(self):
        self.write_log({"ts": self.at(3), "event": "worker_start"}, {"ts": self.at(3), "event": "pass", "result": "ok"})
        s = self.state()
        self.assertEqual((s["state"], s["last_result"], s["age_seconds"]), ("active", "ok", 180))
        self.assertIn("3 min ago", s["detail"])

    def test_old_pass_is_stale(self):
        self.write_log({"ts": self.at(STALE_AFTER // 60 + 5), "event": "pass", "result": "ok"})
        self.assertEqual(self.state()["state"], "stale")

    def test_pause_file_wins_over_everything(self):
        self.write_log({"ts": self.at(1), "event": "pass", "result": "ok"})
        (self.bridge / "PAUSE").write_text("x")
        s = self.state()
        self.assertEqual((s["state"], s["paused"]), ("paused", True))
        self.assertIn("--resume", s["detail"])

    def test_garbage_and_huge_logs_are_tolerated(self):
        self.log.write_text("not json\n" * 50000 + json.dumps({"ts": self.at(1), "event": "pass", "result": "ok"}) + "\n")
        self.assertEqual(self.state()["state"], "active")
        self.log.write_bytes(b"\xff\xfe\x00 binary junk")
        self.assertEqual(self.state()["state"], "unknown")

    def test_reading_never_modifies_files(self):
        self.write_log({"ts": self.at(1), "event": "pass", "result": "ok"})
        before = self.log.read_bytes()
        self.state()
        self.assertEqual(self.log.read_bytes(), before)
        self.assertFalse((self.bridge / "PAUSE").exists())


class DiagnosticsTests(unittest.TestCase):
    INFO = {
        "version": "0.1.0", "python": "3.12.10", "pyside": "6.12.0", "platform": "Windows-11",
        "provider": {"name": "claude-code", "state": "connected", "detail": "Claude Code CLI signed in (claude.ai)"},
        "store": {"location": r"C:\Users\x\AppData\Local\Jarvis\jarvis.db", "persistent": True, "integrity": "ok",
                  "size_kb": 12.0, "messages": 4, "activity": 9, "notice": ""},
        "worker": {"state": "active", "last_pass": "2026-10-22T14:58:00", "last_result": "ok", "detail": "Last pass 2 min ago."},
        "tasks": {"awaiting": 1, "queued": 0, "running": 0, "blocked": 0, "done": 5},
    }

    def test_summary_content(self):
        text = diagnostics.build(self.INFO)
        for needle in ("Jarvis 0.1.0", "claude-code", "jarvis.db", "awaiting approval: 1", "Approval is human-only"):
            self.assertIn(needle, text)

    def test_secrets_are_scrubbed_even_if_a_caller_passes_them(self):
        info = json.loads(json.dumps(self.INFO))
        info["provider"]["detail"] = "failed with token=abcdef123456 and sk-abcdefghijklmnopqrstuv"
        info["store"]["notice"] = "password: hunter22hunter"
        text = diagnostics.build(info)
        for secret in ("abcdef123456", "sk-abcdefghijklmnopqrstuv", "hunter22hunter"):
            self.assertNotIn(secret, text)

    def test_unsaved_session_is_called_out(self):
        info = json.loads(json.dumps(self.INFO))
        info["store"]["persistent"] = False
        self.assertIn("NO (this session is not being saved)", diagnostics.build(info))


if __name__ == "__main__":
    unittest.main()
