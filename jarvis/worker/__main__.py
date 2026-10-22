"""Run approved bridge tasks.

  python -m jarvis.worker                 one pass (what the scheduler runs)
  python -m jarvis.worker --loop          keep polling in the foreground (Ctrl+C to stop)
  python -m jarvis.worker --status        show pause state, provider, queue and recent log events
  python -m jarvis.worker --pause         stop all processing (creates bridge/PAUSE)
  python -m jarvis.worker --resume        remove bridge/PAUSE
"""
import argparse
import json
import sys
import time

from jarvis.integrations.claude_cli import ClaudeCodeProvider
from jarvis.worker.log import DEFAULT_LOG, JsonLog
from jarvis.worker.worker import DEFAULT_TASKS_DIR, ROOT, TaskWorker


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=15, help="seconds between polls with --loop")
    ap.add_argument("--allow-file-changes", action="store_true",
                    help="also run approved file-changes tasks (Edit/Write, never in protected paths)")
    ap.add_argument("--allow-commands", action="store_true",
                    help="also run approved commands tasks, limited to bridge/command_allowlist.json")
    ap.add_argument("--task-timeout", type=int, default=900, help="seconds before one task run is abandoned")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--pause", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--workdir", default=str(ROOT), help="directory Claude Code runs in (default: the project)")
    ap.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR))
    ap.add_argument("--log-file", default=str(DEFAULT_LOG))
    a = ap.parse_args()
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")

    log = JsonLog(a.log_file)
    worker = TaskWorker(ClaudeCodeProvider(cwd=a.workdir, timeout=a.task_timeout), tasks_dir=a.tasks_dir,
                        allow_file_changes=a.allow_file_changes, allow_commands=a.allow_commands,
                        max_attempts=a.max_attempts, log=log)
    if a.pause:
        worker.pause_file.write_text("paused by human\n", encoding="utf-8")
        log.event("paused")
        print("paused")
        return 0
    if a.resume:
        worker.pause_file.unlink(missing_ok=True)
        log.event("resumed")
        print("resumed")
        return 0
    if a.status:
        tasks = worker._all()
        print(json.dumps({
            "paused": worker.paused,
            "provider": worker.provider.status().__dict__,
            "pending_approved": len(worker.eligible()),
            "pending_unapproved": sum(1 for t in tasks if t.get("status") == "pending" and not t.get("approved")),
            "in_progress": sum(1 for t in tasks if t.get("status") == "in-progress"),
            "recent_log": log.tail(5),
        }, indent=2))
        return 0

    log.event("worker_start", file_changes=a.allow_file_changes, commands=a.allow_commands, loop=a.loop)
    try:
        while True:
            for o in worker.process_once():
                if sys.stdout is not None:
                    print(f"[{o.action}] task {o.task_id}: {o.detail}", flush=True)
            if not a.loop:
                return 0
            time.sleep(a.interval)
    except KeyboardInterrupt:
        log.event("worker_stop")
        return 0


if __name__ == "__main__":
    sys.exit(main())
