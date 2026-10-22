import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
import coordination
import tasklock


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = Path(self.tmp.name) / "tasks"
        self.tasks.mkdir()
        self.state = Path(self.tmp.name) / coordination.STATE_FILE
        self.calls = []

    def put(self, ident=1, **extra):
        task = {"id": ident, "title": "parent", "body": "original", "status": "pending", "approved": False,
                "needs": "read-only", "source": "mcp", "updated": "2026-01-01 00:00:00", "notes": [], **extra}
        (self.tasks / f"{ident:03d}.json").write_text(json.dumps(task), encoding="utf-8")
        return task

    def submit(self, request):
        self.calls.append(request)
        return {"id": 99, "approved": True}

    def test_completed_event_is_recorded_once(self):
        self.put(status="done", result="ok", completed_run_id="run")
        self.assertEqual(self.process()[0]["state"], "completed")
        self.assertEqual(self.process(), [])

    def test_recoverable_failure_submits_one_same_tier_followup(self):
        self.put(last_error="temporary", attempts=1)
        out = self.process()
        self.assertEqual(out[0]["state"], "followup_submitted")
        self.assertEqual(self.calls[0]["needs"], "read-only")
        self.assertIn("original", self.calls[0]["body"])
        self.assertEqual(self.process(), [])

    def test_retry_exhaustion_does_not_submit(self):
        self.put(last_error="temporary", attempts=1, followup_attempt=2)
        self.assertEqual(self.process()[0]["state"], "retry_exhausted")
        self.assertEqual(self.calls, [])

    def test_restart_uses_persisted_state_without_duplicate(self):
        self.put(last_error="temporary", attempts=1)
        self.process()
        self.assertEqual(coordination.process_once(self.tasks, self.state, self.submit), [])
        self.assertEqual(len(self.calls), 1)

    def test_restart_after_submission_recovers_existing_child(self):
        self.put(last_error="temporary", attempts=1)
        child = self.put(2, source="mcp-followup", parent_id=1, status="pending", approved=True)
        child["updated"] = "2026-01-01 00:00:01"
        (self.tasks / "002.json").write_text(json.dumps(child), encoding="utf-8")
        def duplicate(_request):
            raise ValueError("a follow-up already exists for this task")
        out = coordination.process_once(self.tasks, self.state, duplicate)
        self.assertEqual((out[0]["state"], out[0]["followup_id"]), ("followup_submitted", 2))

    def test_pause_waits_then_continues_after_resume(self):
        self.put(last_error="temporary", attempts=1)
        pause = self.tasks.parent / "PAUSE"
        pause.write_text("paused", encoding="utf-8")
        self.assertEqual(self.process()[0]["state"], "waiting_policy")
        self.assertEqual(self.calls, [])
        pause.unlink()
        self.assertEqual(self.process()[0]["state"], "followup_submitted")

    def test_concurrent_pass_is_skipped_without_duplicate_submission(self):
        self.put(last_error="temporary", attempts=1)
        lock = tasklock.acquire(self.tasks.parent / "locks" / "coordinator.lock", owner="test")
        self.assertIsNotNone(lock)
        try:
            self.assertEqual(self.process(), [])
            self.assertEqual(self.calls, [])
        finally:
            lock.release()

    def process(self):
        return coordination.process_once(self.tasks, self.state, self.submit)


class CoordinationMcpTests(unittest.TestCase):
    def setUp(self):
        import mcp_server
        self.mcp = mcp_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = self.mcp.bridge.TASKS
        self.mcp.bridge.TASKS = Path(self.tmp.name) / "tasks"
        self.mcp.bridge.TASKS.mkdir()
        self.addCleanup(lambda: setattr(self.mcp.bridge, "TASKS", self.old))

    def test_existing_mcp_followup_path_is_used(self):
        task = {"id": 1, "title": "parent", "body": "original", "status": "pending", "approved": False,
                "needs": "read-only", "source": "mcp", "updated": "2026-01-01 00:00:00", "notes": [],
                "last_error": "temporary", "attempts": 1}
        (self.mcp.bridge.TASKS / "001.json").write_text(json.dumps(task), encoding="utf-8")
        result = self.mcp.call("coordinate_task_events", {})
        self.assertEqual(result[0]["state"], "followup_submitted")
        child = json.loads((self.mcp.bridge.TASKS / "002.json").read_text(encoding="utf-8"))
        self.assertEqual((child["parent_id"], child["needs"], child["source"]), (1, "read-only", "mcp-followup"))
        self.assertTrue(child["approved"])
