"""OS-level locks (survive nothing: released when the holder dies), process containment, and admin claim."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
import tasklock  # noqa: E402

from jarvis.integrations.process import run_contained  # noqa: E402
from jarvis.worker.worker import TaskWorker  # noqa: E402
from tests.test_worker import FakeProvider  # noqa: E402

BASE_PY = getattr(sys, "_base_executable", sys.executable)  # the real interpreter, not the venv launcher


def pid_running(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                         capture_output=True, text=True).stdout
    return f'"{pid}"' in out


def wait_until(cond, timeout=8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


class TaskLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.p = Path(self._tmp.name) / "locks" / "001.lock"

    def test_exclusive_then_released(self):
        a = tasklock.acquire(self.p)
        self.assertIsNotNone(a)
        self.assertIsNone(tasklock.acquire(self.p))
        self.assertTrue(tasklock.is_held(self.p))
        self.assertEqual(tasklock.info(self.p)["pid"], os.getpid())   # diagnostics readable while held
        a.release()
        self.assertFalse(tasklock.is_held(self.p))
        b = tasklock.acquire(self.p)
        self.assertIsNotNone(b)
        b.release()

    def test_lock_is_released_when_holder_process_is_killed(self):
        code = (f"import sys,time; sys.path.insert(0, {str(ROOT / 'bridge')!r}); import tasklock; "
                f"l = tasklock.acquire({str(self.p)!r}); print('locked' if l else 'busy', flush=True); time.sleep(120)")
        proc = subprocess.Popen([BASE_PY, "-I", "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        self.assertEqual(proc.stdout.readline().strip(), "locked")
        self.assertTrue(tasklock.is_held(self.p))
        self.assertIsNone(tasklock.acquire(self.p))
        proc.kill()                                   # hard kill: no cleanup code runs in the holder
        proc.wait()
        self.assertTrue(wait_until(lambda: not tasklock.is_held(self.p)), "OS did not release the lock")
        self.assertTrue(self.p.exists())              # the file is left behind, but it is not a lock any more
        again = tasklock.acquire(self.p)
        self.assertIsNotNone(again)
        again.release()


@unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
class ContainmentTests(unittest.TestCase):
    SPAWN = ("import subprocess,sys,time; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
             "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(g.pid, flush=True); {tail}")

    def test_timeout_kills_the_whole_child_tree(self):
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            run_contained([BASE_PY, "-c", self.SPAWN.format(tail="time.sleep(120)")], timeout=3)
        grandchild = int(cm.exception.output.split()[0])
        self.assertTrue(wait_until(lambda: not pid_running(grandchild)), "grandchild survived the timeout")

    def test_leftover_processes_die_when_the_run_ends(self):
        r = run_contained([BASE_PY, "-c", self.SPAWN.format(tail="")], timeout=30)   # child exits, grandchild lingers
        self.assertTrue(r.contained)
        self.assertEqual(r.returncode, 0)
        grandchild = int(r.stdout.split()[0])
        self.assertTrue(wait_until(lambda: not pid_running(grandchild)), "lingering grandchild was not killed")

    def test_input_and_output_round_trip(self):
        r = run_contained([BASE_PY, "-c", "import sys; print(sys.stdin.read().upper())"], input="héllo", timeout=30)
        self.assertEqual(r.stdout.strip(), "HÉLLO")


class AdminClaimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tasks = Path(self._tmp.name) / "bridge" / "tasks"
        self.tasks.mkdir(parents=True)
        for i, approved in ((1, True), (2, False)):
            (self.tasks / f"{i:03d}.json").write_text(json.dumps(
                {"id": i, "title": f"t{i}", "body": "b", "status": "pending", "created": "x", "updated": "x",
                 "notes": [], "approved": approved, "needs": "read-only"}), encoding="utf-8")

    def get(self, i):
        return json.loads((self.tasks / f"{i:03d}.json").read_text(encoding="utf-8"))

    def admin(self, *args):
        return subprocess.run([BASE_PY, "-I", str(ROOT / "bridge" / "admin.py"), *args], capture_output=True,
                              text=True, env={**os.environ, "BRIDGE_TASKS_DIR": str(self.tasks)})

    def test_claimed_task_is_never_picked_up_or_recovered_by_the_worker(self):
        r = self.admin("claim", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        t = self.get(1)
        self.assertEqual((t["status"], t["claimed_by"]), ("in-progress", "interactive"))
        p = FakeProvider()
        self.assertEqual(TaskWorker(p, tasks_dir=self.tasks).process_once(), [])
        self.assertEqual(p.calls, [])
        self.assertEqual(self.get(1)["status"], "in-progress")

    def test_claim_refused_while_the_worker_holds_the_task(self):
        h = tasklock.acquire(self.tasks.parent / "locks" / "001.lock")
        self.addCleanup(h.release)
        r = self.admin("claim", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("background worker", r.stderr)
        self.assertEqual(self.get(1)["status"], "pending")

    def test_claim_refused_for_unapproved_task(self):
        r = self.admin("claim", "2")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.get(2)["status"], self.get(2)["approved"]), ("pending", False))


if __name__ == "__main__":
    unittest.main()
