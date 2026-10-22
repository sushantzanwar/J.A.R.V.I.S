import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bridge" / "coordinator.py"


class CoordinatorCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = Path(self.tmp.name) / "tasks"
        self.tasks.mkdir()
        self.log = Path(self.tmp.name) / "coordinator.jsonl"

    def put_failure(self):
        task = {"id": 1, "title": "parent", "body": "original", "status": "pending", "approved": False,
                "needs": "read-only", "source": "mcp", "updated": "2026-01-01 00:00:00", "notes": [],
                "last_error": "temporary", "attempts": 1}
        (self.tasks / "001.json").write_text(json.dumps(task), encoding="utf-8")

    def run_cli(self):
        env = {**os.environ, "BRIDGE_TASKS_DIR": str(self.tasks)}
        return subprocess.run([sys.executable, str(SCRIPT), "--log-file", str(self.log)], cwd=ROOT,
                              env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)

    def events(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_startup_clean_shutdown_and_restart_are_persistent(self):
        self.put_failure()
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((self.tasks / "002.json").exists())
        second = self.run_cli()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse((self.tasks / "003.json").exists())
        names = [event["event"] for event in self.events()]
        self.assertIn("coordinator_start", names)
        self.assertIn("coordinator_stop", names)

    def test_pause_records_waiting_policy_without_creating_child(self):
        self.put_failure()
        (self.tasks.parent / "PAUSE").write_text("paused", encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.tasks / "002.json").exists())
        outcomes = [event for event in self.events() if event["event"] == "coordinator_outcome"]
        self.assertEqual(outcomes[0]["state"], "waiting_policy")

    def test_failure_is_logged_and_returns_nonzero(self):
        # A directory where the atomic state file belongs forces a safe write
        # failure without touching a live bridge. The next scheduler pass can
        # retry once that external filesystem condition is corrected.
        (self.tasks.parent / "coordination_state.json").mkdir()
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.events()[-1]["event"], "coordinator_error")


class SchedulerScriptTests(unittest.TestCase):
    def test_existing_scheduler_infrastructure_registers_supervised_coordinator(self):
        text = (ROOT / "scripts" / "jarvis-scheduler.ps1").read_text(encoding="utf-8")
        self.assertIn("JarvisWorker", text)
        self.assertIn("JarvisCoordinator", text)
        self.assertIn("-MultipleInstances IgnoreNew", text)
        self.assertIn("-StartWhenAvailable", text)
        self.assertIn("coordinator has unexpected principal", text)
        self.assertIn("coordinator.jsonl", text)
