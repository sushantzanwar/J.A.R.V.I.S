import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
import result_monitor


class ResultMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = Path(self.tmp.name) / "tasks"
        self.tasks.mkdir()
        self.state = Path(self.tmp.name) / "monitor.json"

    def put(self, ident, **extra):
        task = {"id": ident, "title": "t", "body": "b", "status": "pending", "approved": False,
                "needs": "read-only", "source": "mcp", "updated": "2026-01-01 00:00:00", "notes": [], **extra}
        (self.tasks / f"{ident:03d}.json").write_text(json.dumps(task), encoding="utf-8")
        return task

    def test_completion_is_persisted_once(self):
        self.put(1, status="done", result="ok", completed_run_id="run")
        first = result_monitor.scan(self.tasks, self.state)
        self.assertEqual(first[0]["kind"], "completed")
        self.assertEqual(result_monitor.scan(self.tasks, self.state), [])
        self.assertEqual(result_monitor.events(self.tasks, self.state)[0]["detail"], "ok")

    def test_recoverable_failure_is_detected_but_non_failure_is_not(self):
        self.put(1, last_error="timeout", attempts=1)
        self.put(2)
        events = result_monitor.scan(self.tasks, self.state)
        self.assertEqual([(e["task_id"], e["kind"]) for e in events], [(1, "recoverable_failure")])


class FollowupMcpTests(unittest.TestCase):
    def setUp(self):
        import mcp_server
        self.mcp = mcp_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = self.mcp.bridge.TASKS
        self.mcp.bridge.TASKS = Path(self.tmp.name) / "tasks"
        self.mcp.bridge.TASKS.mkdir()
        self.addCleanup(lambda: setattr(self.mcp.bridge, "TASKS", self.old))

    def failed_parent(self, ident=1, attempt=0):
        task = {"id": ident, "title": "parent", "body": "b", "status": "pending", "approved": False,
                "needs": "read-only", "source": "mcp", "updated": "2026-01-01 00:00:00", "notes": [],
                "last_error": "temporary failure", "attempts": 1, "followup_attempt": attempt}
        (self.mcp.bridge.TASKS / f"{ident:03d}.json").write_text(json.dumps(task), encoding="utf-8")

    def test_followup_is_auto_approved_once_and_keeps_tier(self):
        self.failed_parent()
        child = self.mcp.call("submit_followup", {"parent_id": 1, "title": "retry", "body": "retry safely"})
        self.assertTrue(child["approved"])
        self.assertEqual(child["needs"], "read-only")
        with self.assertRaises(ValueError):
            self.mcp.call("submit_followup", {"parent_id": 1, "title": "again", "body": "x"})

    def test_followup_cannot_escalate_or_exceed_limit(self):
        self.failed_parent(attempt=2)
        with self.assertRaises(ValueError):
            self.mcp.call("submit_followup", {"parent_id": 1, "title": "retry", "body": "x"})
        self.failed_parent(ident=2)
        with self.assertRaises(ValueError):
            self.mcp.call("submit_followup", {"parent_id": 2, "title": "retry", "body": "x", "needs": "commands"})

    def test_pause_prevents_auto_approval_of_followup(self):
        self.failed_parent()
        (self.mcp.bridge.TASKS.parent / "PAUSE").write_text("x", encoding="utf-8")
        child = self.mcp.call("submit_followup", {"parent_id": 1, "title": "retry", "body": "x"})
        self.assertFalse(child["approved"])
