import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from jarvis.core.providers import CONNECTED, DISCONNECTED, ModelProvider, ProviderError, ProviderStatus
from jarvis.worker import worker as worker_mod
from jarvis.worker.worker import TaskWorker


class FakeProvider(ModelProvider):
    name = "fake"

    def __init__(self, state=CONNECTED, result="done it", exc=None):
        self.state, self.result, self.exc, self.calls = state, result, exc, []

    def status(self):
        return ProviderStatus(self.state, "fake")

    def generate(self, prompt):
        return self.result

    def run_task(self, prompt, tools, disallowed=(), allowed=None):
        self.calls.append({"prompt": prompt, "tools": tuple(tools), "disallowed": tuple(disallowed),
                           "allowed": None if allowed is None else tuple(allowed)})
        if self.exc:
            raise self.exc
        return self.result


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def add(self, i, status="pending", approved=False, needs="read-only", **extra):
        t = {"id": i, "title": f"task {i}", "body": "do the thing", "status": status, "created": "x",
             "updated": "x", "notes": [], "approved": approved, "needs": needs, **extra}
        (self.tasks / f"{i:03d}.json").write_text(json.dumps(t), encoding="utf-8")

    def get(self, i):
        return json.loads((self.tasks / f"{i:03d}.json").read_text(encoding="utf-8"))

    def worker(self, provider=None, **kw):
        self.provider = provider or FakeProvider()
        return TaskWorker(self.provider, tasks_dir=self.tasks, **kw)


class LifecycleTests(WorkerCase):
    def test_runs_only_approved_pending_tasks_and_records_result(self):
        self.add(1, approved=True)
        self.add(2, approved=False)                   # not approved
        self.add(3, status="done", approved=True)     # already finished
        self.add(4, status="in-progress", approved=True)  # someone else's
        out = self.worker().process_once()
        self.assertEqual([(o.task_id, o.action) for o in out], [(1, "done")])
        t = self.get(1)
        self.assertEqual((t["status"], t["result"]), ("done", "done it"))
        self.assertTrue(any("worker finished" in n["text"] for n in t["notes"]))
        for i in (2, 3, 4):
            self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.get(2)["status"], "pending")
        self.assertEqual(self.get(4)["status"], "in-progress")

    def test_result_is_redacted(self):
        self.add(1, approved=True)
        self.worker(FakeProvider(result="key api_key=abcdef123456 ok")).process_once()
        self.assertNotIn("abcdef123456", self.get(1)["result"])

    def test_prompt_contains_task(self):
        self.add(1, approved=True)
        w = self.worker()
        w.process_once()
        self.assertIn("do the thing", self.provider.calls[0]["prompt"])


class DuplicatePreventionTests(WorkerCase):
    def test_finished_task_is_not_rerun(self):
        self.add(1, approved=True)
        w = self.worker()
        w.process_once()
        w.process_once()
        self.assertEqual(len(self.provider.calls), 1)

    def test_existing_live_lock_blocks_execution(self):
        self.add(1, approved=True)
        w = self.worker()
        h = w._acquire(1)                              # another run holds the claim
        self.assertIsNotNone(h)
        self.addCleanup(h.release)
        out = w.process_once()
        self.assertEqual(out[0].action, "skipped")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.get(1)["status"], "pending")

    def test_concurrent_workers_run_task_once(self):
        self.add(1, approved=True)
        gate = threading.Event()

        class Slow(FakeProvider):
            def run_task(self, *a, **k):
                gate.wait(2)
                return super().run_task(*a, **k)

        p = Slow()
        ws = [TaskWorker(p, tasks_dir=self.tasks) for _ in range(4)]
        outs = []
        ts = [threading.Thread(target=lambda w=w: outs.append(w.run_task(1))) for w in ws]
        [t.start() for t in ts]
        threading.Timer(0.3, gate.set).start()
        [t.join() for t in ts]
        self.assertEqual(len(p.calls), 1)
        self.assertEqual(sorted(o.action for o in outs), ["done", "skipped", "skipped", "skipped"])

    def test_lock_released_after_run(self):
        self.add(1, approved=True)
        self.worker().process_once()
        self.assertEqual(list((self.root / "locks").glob("*.lock")), [])


class FailureRecoveryTests(WorkerCase):
    def test_provider_failure_resets_and_requires_reapproval_when_retries_exhausted(self):
        self.add(1, approved=True)
        w = self.worker(FakeProvider(exc=ProviderError("boom")), max_attempts=1)
        out = w.process_once()
        self.assertEqual(out[0].action, "failed")
        t = self.get(1)
        self.assertEqual((t["status"], t["approved"], t["attempts"]), ("pending", False, 1))
        self.assertTrue(any("boom" in n["text"] for n in t["notes"]))
        w.process_once()                               # no automatic retry
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(list((self.root / "locks").glob("*.lock")), [])

    def test_unexpected_exception_fails_closed(self):
        self.add(1, approved=True)
        w = self.worker(FakeProvider(exc=RuntimeError("secret detail")))
        w.process_once()
        t = self.get(1)
        self.assertEqual((t["status"], t["approved"]), ("pending", False))
        self.assertNotIn("secret detail", json.dumps(t["notes"]))

    def test_interrupted_file_changes_task_is_reset_unapproved(self):
        self.add(1, status="in-progress", approved=True, needs="file-changes", claimed_by="worker")  # crashed, no lock
        out = self.worker(allow_file_changes=True).process_once()
        self.assertEqual([o.action for o in out], ["recovered"])
        t = self.get(1)
        self.assertEqual((t["status"], t["approved"]), ("pending", False))
        self.assertEqual(self.provider.calls, [])      # not re-run without a fresh human approval

    def test_dead_lock_is_cleared(self):
        self.add(1, approved=True)
        w = self.worker()
        w.lock_dir.mkdir()
        (w.lock_dir / "001.lock").write_text(json.dumps({"pid": 99999999, "at": 0}))
        out = w.process_once()
        self.assertEqual(out[0].action, "done")

    def test_manual_in_progress_task_is_never_touched(self):
        self.add(1, status="in-progress", approved=True)        # claimed by a person / interactive Claude
        self.worker().process_once()
        t = self.get(1)
        self.assertEqual((t["status"], t["approved"]), ("in-progress", True))


class ProviderDisconnectedTests(WorkerCase):
    def test_disconnected_provider_touches_nothing(self):
        self.add(1, approved=True)
        before = (self.tasks / "001.json").read_bytes()
        out = self.worker(FakeProvider(state=DISCONNECTED)).process_once()
        self.assertEqual(out[0].action, "skipped")
        self.assertIn("disconnected", out[0].detail)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual((self.tasks / "001.json").read_bytes(), before)


class SafetyBoundaryTests(WorkerCase):
    def test_never_self_approves(self):
        self.add(1, approved=False)
        self.add(2, approved=False, needs="file-changes")
        w = self.worker(allow_file_changes=True)
        w.process_once()
        w.process_once()
        for i in (1, 2):
            self.assertEqual((self.get(i)["status"], self.get(i)["approved"]), ("pending", False))
        self.assertEqual(self.provider.calls, [])

    def test_read_only_gets_no_write_tools(self):
        self.add(1, approved=True, needs="read-only")
        self.worker(allow_file_changes=True).process_once()
        call = self.provider.calls[0]
        self.assertEqual(call["tools"], ("Read", "Glob", "Grep"))
        self.assertNotIn("Bash", call["tools"])

    def test_file_changes_skipped_without_flag(self):
        self.add(1, approved=True, needs="file-changes")
        out = self.worker().process_once()
        self.assertEqual(out[0].action, "skipped")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.get(1)["status"], "pending")

    def test_file_changes_with_flag_gets_edit_but_protected_paths_denied(self):
        self.add(1, approved=True, needs="file-changes")
        self.worker(allow_file_changes=True).process_once()
        call = self.provider.calls[0]
        self.assertEqual(call["tools"], ("Read", "Glob", "Grep", "Edit", "Write"))
        for path in ("bridge/**", "jarvis/worker/**", ".git/**"):
            self.assertIn(f"Edit({path})", call["disallowed"])
            self.assertIn(f"Write({path})", call["disallowed"])

    def test_commands_skipped_without_flag(self):
        self.add(1, approved=True, needs="commands")
        out = self.worker(allow_file_changes=True).process_once()    # file-changes flag must not imply commands
        self.assertEqual(out[0].action, "skipped")
        self.assertIn("--allow-commands", out[0].detail)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.get(1)["status"], "pending")

    def test_commands_skipped_without_allowlist_file(self):
        self.add(1, approved=True, needs="commands")
        out = self.worker(allow_commands=True).process_once()
        self.assertEqual(out[0].action, "skipped")
        self.assertIn("allowlist", out[0].detail)
        self.assertEqual(self.provider.calls, [])

    def test_commands_refused_when_allowlist_contains_forbidden_rule(self):
        self.add(1, approved=True, needs="commands")
        for bad in ("Bash(python bridge/admin.py approve *)", "Bash(schtasks *)", "Bash(*)", "Bash(curl http://x *)",
                    "Bash(pip install *)", "Bash(git push *)", "Bash(echo a && rm -rf x)", "Read(.env)"):
            (self.root / "command_allowlist.json").write_text(json.dumps({"allow": [bad]}))
            out = self.worker(allow_commands=True).process_once()
            self.assertEqual(out[0].action, "skipped", bad)
        self.assertEqual(self.provider.calls, [])

    def test_commands_run_only_allowlisted_rules(self):
        self.add(1, approved=True, needs="commands")
        rules = ["Bash(.venv/Scripts/python.exe -m unittest *)", "Bash(git status *)"]
        (self.root / "command_allowlist.json").write_text(json.dumps({"allow": rules}))
        out = self.worker(allow_commands=True).process_once()
        self.assertEqual(out[0].action, "done")
        call = self.provider.calls[0]
        self.assertIn("Bash", call["tools"])
        self.assertNotIn("Edit", call["tools"])
        self.assertEqual(call["allowed"], ("Read", "Glob", "Grep", *rules))   # no bare "Bash" allowed
        self.assertIn("Bash(*admin.py*)", call["disallowed"])
        self.assertIn("Bash(*schtasks*)", call["disallowed"])

    def test_example_allowlist_is_valid(self):
        from jarvis.worker import allowlist
        example = Path(worker_mod.__file__).resolve().parents[2] / "bridge" / "command_allowlist.example.json"
        self.assertGreater(len(allowlist.load(example)), 3)

    def test_unknown_permission_level_blocked(self):
        self.add(1, approved=True, needs="root")
        self.worker().process_once()
        self.assertEqual(self.get(1)["status"], "blocked")
        self.assertEqual(self.provider.calls, [])

    def test_source_has_no_approval_or_shell_paths(self):
        src = Path(worker_mod.__file__).read_text(encoding="utf-8")
        # no assignment to an "approved" key anywhere may involve True (only reads / resets to False)
        self.assertIsNone(re.search(r'\["approved"\][^\n]*=[^\n]*True', src))
        self.assertIsNone(re.search(r'"approved"\s*:\s*True', src))
        self.assertNotIn("shell=True", src)
        self.assertNotIn("import admin", src)
        self.assertNotIn("subprocess", src)


class RetryTests(WorkerCase):
    def setUp(self):
        super().setUp()
        self.now = [1000.0]

    def w(self, provider, **kw):
        return self.worker(provider, clock=lambda: self.now[0], backoff_base=60, **kw)

    def test_read_only_retries_with_backoff_then_gives_up(self):
        self.add(1, approved=True)
        w = self.w(FakeProvider(exc=ProviderError("rate limited")), max_attempts=3)
        self.assertEqual(w.process_once()[0].action, "retry")
        t = self.get(1)
        self.assertEqual((t["status"], t["approved"], t["attempts"]), ("pending", True, 1))   # still approved, same tier
        self.assertEqual(t["next_attempt_at"], 1060.0)
        self.assertEqual(w.process_once(), [])                       # inside backoff: nothing runs
        self.assertEqual(len(self.provider.calls), 1)
        self.now[0] += 61
        self.assertEqual(w.process_once()[0].action, "retry")        # attempt 2, backoff doubles
        self.assertEqual(self.get(1)["next_attempt_at"], self.now[0] + 120)
        self.now[0] += 121
        self.assertEqual(w.process_once()[0].action, "failed")       # attempt 3 exhausts the bound
        t = self.get(1)
        self.assertEqual((t["status"], t["approved"], t["attempts"]), ("pending", False, 3))
        self.now[0] += 10_000
        self.assertEqual(w.process_once(), [])                       # needs a human now
        self.assertEqual(len(self.provider.calls), 3)

    def test_retry_then_success_records_one_completed_run(self):
        self.add(1, approved=True)

        class Flaky(FakeProvider):
            def run_task(self, *a, **k):
                r = super().run_task(*a, **k)
                if len(self.calls) == 1:
                    raise ProviderError("temporary")
                return r

        w = self.w(Flaky(), max_attempts=3)
        w.process_once()
        self.now[0] += 61
        self.assertEqual(w.process_once()[0].action, "done")
        t = self.get(1)
        self.assertEqual((t["status"], t["result"]), ("done", "done it"))
        self.assertEqual(t["completed_run_id"], t["last_run_id"])
        self.assertNotIn("last_error", t)
        self.now[0] += 10_000
        self.assertEqual(w.process_once(), [])

    def test_side_effecting_tiers_are_never_auto_retried(self):
        self.add(1, approved=True, needs="file-changes")
        w = self.w(FakeProvider(exc=ProviderError("timeout")), allow_file_changes=True)
        self.assertEqual(w.process_once()[0].action, "failed")
        t = self.get(1)
        self.assertEqual((t["approved"], t["attempts"]), (False, 1))
        self.now[0] += 10_000
        self.assertEqual(w.process_once(), [])
        self.assertEqual(len(self.provider.calls), 1)

    def test_unexpected_error_is_not_retried(self):
        self.add(1, approved=True)
        w = self.w(FakeProvider(exc=RuntimeError("x")), max_attempts=5)
        self.assertEqual(w.process_once()[0].action, "failed")
        self.assertFalse(self.get(1)["approved"])


class RestartRecoveryTests(WorkerCase):
    def test_read_only_interrupted_task_is_requeued_and_completes_after_restart(self):
        self.add(1, status="in-progress", approved=True, claimed_by="worker", attempts=0)   # old worker died mid-run
        w = self.worker()                                  # "restart": a brand-new worker instance
        out = w.process_once()
        self.assertEqual([o.action for o in out], ["recovered", "done"])
        t = self.get(1)
        self.assertEqual((t["status"], t["attempts"]), ("done", 1))
        self.assertEqual(len(self.provider.calls), 1)

    def test_recovery_attempts_are_bounded(self):
        self.add(1, status="in-progress", approved=True, claimed_by="worker", attempts=2)
        out = self.worker(max_attempts=3).process_once()
        self.assertEqual([o.action for o in out], ["recovered"])
        self.assertFalse(self.get(1)["approved"])
        self.assertEqual(self.provider.calls, [])

    def test_live_lock_of_running_worker_is_not_recovered(self):
        self.add(1, status="in-progress", approved=True, claimed_by="worker")
        w = self.worker()
        h = w._acquire(1)                                  # a live worker still holds it
        self.assertIsNotNone(h)
        self.addCleanup(h.release)
        self.assertEqual(w.recover(), [])
        self.assertEqual(self.get(1)["status"], "in-progress")


class PassControlTests(WorkerCase):
    def test_overlapping_passes_do_not_both_run(self):
        self.add(1, approved=True)
        w = self.worker()
        h = w._acquire("worker")                            # a pass is already running
        self.assertIsNotNone(h)
        self.addCleanup(h.release)
        out = w.process_once()
        self.assertEqual(out[0].action, "skipped")
        self.assertIn("another worker pass", out[0].detail)
        self.assertEqual(self.provider.calls, [])

    def test_pause_file_stops_processing_and_resume_restores(self):
        self.add(1, approved=True)
        w = self.worker()
        w.pause_file.write_text("x")
        self.assertIn("paused", w.process_once()[0].detail)
        self.assertEqual(self.provider.calls, [])
        w.pause_file.unlink()
        self.assertEqual(w.process_once()[0].action, "done")

    def test_idle_pass_does_not_start_the_provider(self):
        self.add(1, approved=False)
        w = self.worker()
        calls = []
        w.provider.status = lambda: calls.append(1) or ProviderStatus(CONNECTED, "x")
        self.assertEqual(w.process_once(), [])
        self.assertEqual(calls, [])


class LoggingTests(WorkerCase):
    def test_structured_log_events_and_redaction(self):
        from jarvis.worker.log import JsonLog
        log = JsonLog(self.root / "logs" / "worker.jsonl")
        self.add(1, approved=True)
        w = self.worker(FakeProvider(exc=ProviderError("denied api_key=abcdef123456")), max_attempts=1, log=log)
        w.process_once()
        raw = (self.root / "logs" / "worker.jsonl").read_text(encoding="utf-8")
        events = [json.loads(x) for x in raw.splitlines()]
        names = [e["event"] for e in events]
        for n in ("pass", "started", "failed_unapproved", "outcome"):
            self.assertIn(n, names)
        self.assertNotIn("abcdef123456", raw)
        self.assertTrue(all("ts" in e for e in events))

    def test_log_rotates(self):
        from jarvis.worker.log import JsonLog
        log = JsonLog(self.root / "w.jsonl", max_bytes=200, keep=2)
        for i in range(30):
            log.event("e", i=i, pad="x" * 20)
        self.assertTrue((self.root / "w.jsonl.1").exists())
        self.assertFalse((self.root / "w.jsonl.3").exists())


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_mcp_submission_returns_existing_task(self):
        import sys
        sys.path.insert(0, str(Path(worker_mod.__file__).resolve().parents[2] / "bridge"))
        import mcp_server
        with tempfile.TemporaryDirectory() as d:
            old = mcp_server.bridge.TASKS
            mcp_server.bridge.TASKS = Path(d)
            try:
                a = mcp_server.call("submit_task", {"title": "t", "body": "b"})
                b = mcp_server.call("submit_task", {"title": "t", "body": "b"})
                c = mcp_server.call("submit_task", {"title": "t", "body": "different"})
                self.assertEqual(a["id"], b["id"])
                self.assertTrue(b.get("duplicate"))
                self.assertNotEqual(a["id"], c["id"])
                self.assertEqual(len(list(Path(d).glob("*.json"))), 2)
            finally:
                mcp_server.bridge.TASKS = old

    def test_mcp_trusted_safe_tier_is_auto_approved_but_commands_is_not(self):
        import sys
        sys.path.insert(0, str(Path(worker_mod.__file__).resolve().parents[2] / "bridge"))
        import mcp_server
        with tempfile.TemporaryDirectory() as d:
            old = mcp_server.bridge.TASKS
            mcp_server.bridge.TASKS = Path(d)
            try:
                safe = mcp_server.call("submit_task", {"title": "safe", "body": "b", "needs": "read-only"})
                command = mcp_server.call("submit_task", {"title": "command", "body": "b", "needs": "commands"})
                self.assertTrue(safe["approved"])
                self.assertEqual(safe.get("approval_origin"), "autonomy-policy-v1")
                self.assertFalse(command["approved"])
            finally:
                mcp_server.bridge.TASKS = old


if __name__ == "__main__":
    unittest.main()
