"""The scheduled worker runs under pythonw.exe (no console). Any console program it starts directly gets a new
visible window unless it is created with CREATE_NO_WINDOW, so every child process must go through the quiet runners."""
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from jarvis.integrations import process
from jarvis.integrations.process import run_quiet
from jarvis.worker.workspaces import WorkspaceManager

ROOT = Path(__file__).resolve().parents[1]
DIRECT_SPAWN = re.compile(r"subprocess\.(run|Popen|call|check_output|check_call)\b")


class RunQuietTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows-only flag")
    def test_adds_create_no_window_and_keeps_other_flags(self):
        with mock.patch.object(process.subprocess, "run") as run:
            run_quiet(["git", "status"], cwd="x", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        flags = run.call_args.kwargs["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(run.call_args.kwargs["cwd"], "x")

    def test_behaves_like_subprocess_run(self):
        r = run_quiet([sys.executable, "-c", "print('hi'); import sys; sys.exit(3)"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (3, "hi"))
        with self.assertRaises(subprocess.CalledProcessError):
            run_quiet([sys.executable, "-c", "import sys; sys.exit(1)"], check=True, capture_output=True)

    def test_workspace_manager_defaults_to_the_quiet_runner(self):
        self.assertIs(WorkspaceManager.__init__.__defaults__[-1], run_quiet)


class NoDirectSpawnTests(unittest.TestCase):
    def test_no_module_starts_a_child_process_except_through_the_quiet_runners(self):
        offenders = []
        for p in list((ROOT / "jarvis").rglob("*.py")) + [q for q in (ROOT / "bridge").glob("*.py")
                                                             if q.name != "smoke_test.py"]:
            if p.name == "process.py" and p.parent.name == "integrations":
                continue   # the one module allowed to call subprocess directly: it applies CREATE_NO_WINDOW
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if DIRECT_SPAWN.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "start child processes with run_quiet or run_contained")


if __name__ == "__main__":
    unittest.main()
