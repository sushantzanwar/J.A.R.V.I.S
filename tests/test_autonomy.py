import json
import tempfile
import unittest
from pathlib import Path


class AutonomyPolicyTests(unittest.TestCase):
    def setUp(self):
        import sys
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "bridge"))
        import autonomy
        self.autonomy = autonomy
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = Path(self.tmp.name) / "tasks"
        self.tasks.mkdir()

    def policy(self, **updates):
        data = {"version": 1, "enabled": True, "trusted_sources": ["mcp"],
                "auto_approve_needs": ["read-only", "file-changes"], "pause_file": "PAUSE"}
        data.update(updates)
        path = Path(self.tmp.name) / "policy.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return self.autonomy.load(path)

    def test_trusted_read_and_write_tiers_are_eligible(self):
        p = self.policy()
        self.assertTrue(self.autonomy.permits("mcp", "read-only", self.tasks, p))
        self.assertTrue(self.autonomy.permits("mcp", "file-changes", self.tasks, p))

    def test_commands_and_untrusted_sources_never_auto_approve(self):
        p = self.policy()
        self.assertFalse(self.autonomy.permits("mcp", "commands", self.tasks, p))
        self.assertFalse(self.autonomy.permits("manual", "read-only", self.tasks, p))

    def test_pause_and_invalid_policy_fail_closed(self):
        p = self.policy()
        (self.tasks.parent / "PAUSE").write_text("x", encoding="utf-8")
        self.assertFalse(self.autonomy.permits("mcp", "read-only", self.tasks, p))
        invalid = Path(self.tmp.name) / "invalid.json"
        invalid.write_text("not json", encoding="utf-8")
        self.assertFalse(self.autonomy.permits("mcp", "read-only", self.tasks, self.autonomy.load(invalid)))
