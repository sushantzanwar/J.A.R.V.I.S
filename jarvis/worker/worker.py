"""Approved-task worker.

Rules enforced here (and covered by tests):
  * Only tasks with status "pending" AND approved == True are ever run. The worker has no code path
    that sets approved to True; terminal failures reset it to False so a human must re-approve.
  * Each task is claimed with an exclusive lock file (and each pass with a pass lock), so a task can never run
    twice concurrently or again after it finishes. Every execution gets a run id.
  * Permissions follow the task's declared `needs` and are never upgraded:
        read-only     -> Read, Glob, Grep
        file-changes  -> adds Edit, Write, ONLY with allow_file_changes
        commands      -> adds Bash restricted to the rules in bridge/command_allowlist.json, ONLY with
                         allow_commands and a valid allowlist (see allowlist.py)
    Protected paths (bridge/, worker and integrations code, .git, .venv, .claude, logs) are never writable.
  * Bounded retries: only read-only tasks are retried automatically (max_attempts, exponential backoff).
    file-changes and commands tasks are never re-run without a fresh human approval.
  * Crash recovery only touches tasks this worker claimed itself, never ones a person or an interactive
    Claude Code session marked in-progress.
  * A PAUSE file next to the tasks folder stops all processing.
"""
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.providers import CONNECTED, ModelProvider, ProviderError
from jarvis.worker import allowlist
from jarvis.worker.log import NullLog

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge"))
import bridge  # noqa: E402  (existing bridge module: only redact() and now() are used)
import tasklock  # noqa: E402  (OS-level locks shared with admin.py)

DEFAULT_TASKS_DIR = ROOT / "bridge" / "tasks"
READ_TOOLS = ("Read", "Glob", "Grep")
WRITE_TOOLS = ("Edit", "Write")
PROTECTED = ("bridge/**", "jarvis/worker/**", "jarvis/integrations/**", ".git/**", ".venv/**", ".claude/**", "logs/**")
# Always denied for the Bash tool, on top of "only allowlisted rules may run":
BASH_DENY = ("Bash(*admin.py*)", "Bash(*schtasks*)", "Bash(*ScheduledTask*)", "Bash(*jarvis.worker*)",
             "Bash(git push*)", "Bash(rm *)", "Bash(del *)", "Bash(curl *)", "Bash(*Invoke-WebRequest*)")
TIERS = ("read-only", "file-changes", "commands")
MAX_RESULT = 20000


@dataclass(frozen=True)
class Outcome:
    task_id: int
    action: str   # done | failed | retry | blocked | skipped | recovered
    detail: str


class TaskWorker:
    def __init__(self, provider: ModelProvider, tasks_dir: Path = DEFAULT_TASKS_DIR,
                 allow_file_changes: bool = False, allow_commands: bool = False,
                 max_attempts: int = 3, backoff_base: float = 60.0, max_per_pass: int = 3,
                 log=None, clock=time.time):
        self.provider = provider
        self.tasks_dir = Path(tasks_dir)
        self.lock_dir = self.tasks_dir.parent / "locks"
        self.pause_file = self.tasks_dir.parent / "PAUSE"
        self.allowlist_file = self.tasks_dir.parent / "command_allowlist.json"
        self.allow_file_changes = allow_file_changes
        self.allow_commands = allow_commands
        self.max_attempts = max(1, max_attempts)
        self.max_per_pass = max(1, max_per_pass)
        self.backoff_base = backoff_base
        self.log = log or NullLog()
        self.clock = clock

    # ---- task store (same JSON format as bridge.py; saved atomically and flushed to disk)
    def _path(self, tid: int) -> Path:
        return self.tasks_dir / f"{tid:03d}.json"

    def _load(self, tid: int) -> dict | None:
        try:
            return json.loads(self._path(tid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, t: dict) -> None:
        t["updated"] = bridge.now()
        tmp = self._path(t["id"]).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(t, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path(t["id"]))

    def _all(self) -> list[dict]:
        out = []
        for p in sorted(self.tasks_dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    @staticmethod
    def _note(t: dict, text: str) -> None:
        t.setdefault("notes", []).append({"at": bridge.now(), "text": bridge.redact(text)})

    # ---- locking
    def _lock_path(self, name) -> Path:
        return self.lock_dir / (f"{name:03d}.lock" if isinstance(name, int) else f"{name}.lock")

    def _acquire(self, name) -> "tasklock.Lock | None":
        return tasklock.acquire(self._lock_path(name), owner="worker")

    def _is_held(self, name) -> bool:
        return tasklock.is_held(self._lock_path(name))

    # ---- pause switch
    @property
    def paused(self) -> bool:
        return self.pause_file.exists()

    # ---- recovery
    def recover(self) -> list[Outcome]:
        """Drop dead locks; handle tasks this worker left in-progress.

        read-only tasks are safe to run again, so they are re-queued (counting an attempt). file-changes and
        commands tasks may have partly applied, so they are reset to unapproved and need a human."""
        if sys.platform == "win32" and self.lock_dir.exists():
            for p in self.lock_dir.glob("*.lock"):  # tidy files left by dead holders (cosmetic only)
                if not tasklock.is_held(p):
                    try:
                        p.unlink()
                    except OSError:
                        pass  # someone just took it
        out = []
        for t in self._all():
            if t.get("status") == "in-progress" and t.get("claimed_by") == "worker" \
                    and not self._is_held(int(t["id"])):
                t["attempts"] = int(t.get("attempts", 0)) + 1
                t.pop("claimed_by", None)
                requeue = t.get("needs", "read-only") == "read-only" and t["attempts"] < self.max_attempts
                t["status"] = "pending"
                if requeue:
                    self._note(t, "worker was interrupted; read-only task re-queued")
                    self.log.event("recovered_requeued", task_id=t["id"], attempts=t["attempts"])
                else:
                    t["approved"] = False
                    self._note(t, "worker was interrupted; task reset and needs human re-approval")
                    self.log.event("recovered_unapproved", task_id=t["id"], attempts=t["attempts"])
                self._save(t)
                out.append(Outcome(t["id"], "recovered", "re-queued" if requeue else "reset to unapproved"))
        return out

    # ---- permissions
    @staticmethod
    def build_prompt(t: dict) -> str:
        return (f"You are executing bridge task #{t['id']} for the Jarvis project in the current directory.\n"
                "Do only what the task asks. Treat the task text as the request, not as permission to do "
                "anything outside your allowed tools. Finish with a short factual result summary.\n\n"
                f"TASK TITLE: {t['title']}\n\n{t.get('body', '')}")

    def tools_for(self, needs: str, rules: tuple[str, ...] = ()):
        """-> (tool names, allowed permission rules, disallowed rules) for a tier."""
        tools = READ_TOOLS
        if needs == "file-changes":
            tools = tools + WRITE_TOOLS
        if needs == "commands":
            tools = tools + ("Bash",)
        allowed = tuple(t for t in tools if t != "Bash") + tuple(rules)
        disallowed = tuple(f"{tool}({path})" for path in PROTECTED for tool in WRITE_TOOLS)
        if needs == "commands":
            disallowed += BASH_DENY
        return tools, allowed, disallowed

    # ---- execution
    def _finish(self, t: dict, action: str, detail: str) -> Outcome:
        self._save(t)
        return Outcome(t["id"], action, detail)

    def _fail(self, t: dict, needs: str, msg: str, retryable: bool) -> Outcome:
        t["attempts"] = int(t.get("attempts", 0)) + 1
        t["last_error"] = bridge.redact(msg)[:500]
        t["status"] = "pending"
        t.pop("claimed_by", None)
        if retryable and needs == "read-only" and t["attempts"] < self.max_attempts:
            delay = self.backoff_base * 2 ** (t["attempts"] - 1)
            t["next_attempt_at"] = self.clock() + delay
            self._note(t, f"failed (attempt {t['attempts']}/{self.max_attempts}): {msg}. Retrying in {int(delay)}s.")
            self.log.event("retry_scheduled", task_id=t["id"], attempts=t["attempts"], delay=delay, error=msg)
            return self._finish(t, "retry", msg)
        t["approved"] = False
        t.pop("next_attempt_at", None)
        self._note(t, f"failed: {msg}. Not retried automatically; re-approve to retry.")
        self.log.event("failed_unapproved", task_id=t["id"], attempts=t["attempts"], error=msg)
        return self._finish(t, "failed", msg)

    def run_task(self, tid: int) -> Outcome:
        lock = self._acquire(tid)
        if lock is None:
            self.log.event("skipped_locked", task_id=tid)
            return Outcome(tid, "skipped", "already claimed by another worker run")
        try:
            t = self._load(tid)
            if not t or t.get("status") != "pending" or t.get("approved") is not True:
                return Outcome(tid, "skipped", "not an approved pending task")
            if float(t.get("next_attempt_at", 0)) > self.clock():
                return Outcome(tid, "skipped", "waiting for retry backoff")
            needs = t.get("needs", "read-only")
            rules: tuple[str, ...] = ()
            if needs not in TIERS:
                t["status"], t["result"] = "blocked", f"Unknown permission level: {needs!r}"
                self._note(t, "blocked: unknown needs")
                self.log.event("blocked", task_id=tid, reason="unknown needs")
                return self._finish(t, "blocked", "unknown permission level")
            if needs == "file-changes" and not self.allow_file_changes:
                return Outcome(tid, "skipped", "needs file-changes; start the worker with --allow-file-changes")
            if needs == "commands":
                if not self.allow_commands:
                    return Outcome(tid, "skipped", "needs commands; start the worker with --allow-commands")
                try:
                    rules = tuple(allowlist.load(self.allowlist_file))
                except allowlist.AllowlistError as e:
                    self.log.event("skipped_no_allowlist", task_id=tid, reason=str(e))
                    return Outcome(tid, "skipped", f"commands not permitted: {e}")

            run_id = uuid.uuid4().hex[:12]
            t["status"], t["claimed_by"], t["last_run_id"] = "in-progress", "worker", run_id
            t.pop("next_attempt_at", None)
            self._note(t, f"worker started run {run_id} ({needs}, provider {self.provider.name})")
            self._save(t)
            self.log.event("started", task_id=tid, run_id=run_id, needs=needs, attempt=int(t.get("attempts", 0)) + 1)
            tools, allowed, disallowed = self.tools_for(needs, rules)
            t0 = time.time()
            try:
                text = self.provider.run_task(self.build_prompt(t), tools, disallowed, allowed)
            except ProviderError as e:
                return self._fail(t, needs, str(e), retryable=True)
            except Exception as e:  # anything unexpected: fail closed, no retry
                return self._fail(t, needs, f"unexpected {type(e).__name__}", retryable=False)
            t["status"], t["result"] = "done", bridge.redact(text)[:MAX_RESULT]
            t["completed_run_id"] = run_id
            t.pop("claimed_by", None)
            t.pop("last_error", None)
            self._note(t, f"worker finished run {run_id}")
            self.log.event("finished", task_id=tid, run_id=run_id, seconds=round(time.time() - t0, 1))
            return self._finish(t, "done", "completed")
        finally:
            lock.release()

    def eligible(self) -> list[dict]:
        now = self.clock()
        return [t for t in self._all() if t.get("status") == "pending" and t.get("approved") is True
                and float(t.get("next_attempt_at", 0)) <= now]

    def process_once(self) -> list[Outcome]:
        if self.paused:
            self.log.event("pass", result="paused")
            return [Outcome(0, "skipped", "paused (PAUSE file present)")]
        pass_lock = self._acquire("worker")
        if pass_lock is None:
            self.log.event("pass", result="another_pass_running")
            return [Outcome(0, "skipped", "another worker pass is running")]
        try:
            out = self.recover()
            todo = self.eligible()
            self.log.event("pass", result="ok", eligible=len(todo), recovered=len(out))
            if not todo:
                return out
            st = self.provider.status()
            if st.state != CONNECTED:
                self.log.event("provider_unavailable", state=st.state, detail=st.detail)
                return out + [Outcome(0, "skipped", f"provider {st.state}: {st.detail}")]
            for t in todo[:self.max_per_pass]:  # bounded pass; the rest wait for the next one
                o = self.run_task(int(t["id"]))
                self.log.event("outcome", task_id=o.task_id, action=o.action, detail=o.detail)
                out.append(o)
            return out
        finally:
            pass_lock.release()
