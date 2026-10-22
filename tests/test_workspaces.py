import subprocess
import tempfile
import unittest
from pathlib import Path

from jarvis.worker.workspaces import WorkspaceError, WorkspaceManager


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        # Runtime worktrees and the integration lock are deliberately ignored
        # in the real project too; neither represents user work.
        (self.root / ".gitignore").write_text(".jarvis/\nbridge/locks/\n__pycache__/\n*.pyc\n", encoding="utf-8")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.manager = WorkspaceManager(self.root)

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=str(cwd or self.root), check=check, capture_output=True,
                              text=True, encoding="utf-8")

    def checkpoint(self, ws, filename, content):
        (ws.path / filename).write_text(content, encoding="utf-8")
        return self.manager.checkpoint_and_verify(ws)

    def test_success_isolated_until_clean_merge(self):
        ws = self.manager.prepare(1)
        self.assertFalse((self.root / "feature.txt").exists())
        commit = self.checkpoint(ws, "feature.txt", "ok\n")
        merged = self.manager.integrate(ws)
        self.assertEqual(merged, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertTrue(commit)
        self.assertEqual((self.root / "feature.txt").read_text(), "ok\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_failed_verification_preserves_workspace_and_main(self):
        ws = self.manager.prepare(2)
        (ws.path / "tests" / "test_bad.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail()\n", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            self.manager.checkpoint_and_verify(ws)
        self.assertTrue((ws.path / "tests" / "test_bad.py").exists())
        self.assertFalse((self.root / "tests" / "test_bad.py").exists())
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_recovery_reuses_same_workspace_without_discarding_changes(self):
        first = self.manager.prepare(3)
        (first.path / "saved.txt").write_text("keep\n", encoding="utf-8")
        second = self.manager.prepare(3, {"path": str(first.path), "branch": first.branch})
        self.assertEqual(first.path, second.path)
        self.assertEqual((second.path / "saved.txt").read_text(), "keep\n")

    def test_concurrent_task_workspaces_do_not_touch_main(self):
        one, two = self.manager.prepare(4), self.manager.prepare(5)
        self.checkpoint(one, "one.txt", "1\n")
        self.checkpoint(two, "two.txt", "2\n")
        self.manager.integrate(one)
        self.manager.integrate(two)
        self.assertEqual((self.root / "one.txt").read_text(), "1\n")
        self.assertEqual((self.root / "two.txt").read_text(), "2\n")

    def test_conflict_aborts_and_preserves_task_workspace(self):
        one, two = self.manager.prepare(6), self.manager.prepare(7)
        self.checkpoint(one, "base.txt", "one\n")
        self.checkpoint(two, "base.txt", "two\n")
        self.manager.integrate(one)
        with self.assertRaisesRegex(WorkspaceError, "merge conflict"):
            self.manager.integrate(two)
        self.assertEqual((self.root / "base.txt").read_text(), "one\n")
        self.assertEqual((two.path / "base.txt").read_text(), "two\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_dirty_target_is_never_reset_or_merged(self):
        ws = self.manager.prepare(8)
        self.checkpoint(ws, "feature.txt", "ok\n")
        (self.root / "user.txt").write_text("user work\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "not clean"):
            self.manager.integrate(ws)
        self.assertEqual((self.root / "user.txt").read_text(), "user work\n")
        self.assertFalse((self.root / "feature.txt").exists())
