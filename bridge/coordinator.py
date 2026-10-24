"""Run the persistent local result coordinator without starting a worker.

Use one pass from an active Codex workflow, or ``--loop`` in a supervised local
session. This process cannot wake an inactive Codex chat.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bridge
import coordination
import mcp_server
import review
from jarvis.worker.log import JsonLog


def run_once():
    return coordination.process_once(
        bridge.TASKS, bridge.TASKS.parent / coordination.STATE_FILE,
        lambda request: mcp_server.call("submit_followup", request),
        prepare_review=lambda task: review.prepare(task, bridge.TASKS.parent, bridge.TASKS.parent.parent),
        create_correction=mcp_server.create_review_correction,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="poll until interrupted")
    parser.add_argument("--interval", type=int, default=30, help="seconds between --loop polls")
    parser.add_argument("--max-idle-polls", type=int, default=20,
                        help="stop --loop after this many idle or paused polls (default: 20)")
    parser.add_argument("--log-file", default=str(Path(__file__).resolve().parents[1] / "logs" / "coordinator.jsonl"))
    args = parser.parse_args()
    log = JsonLog(args.log_file)
    log.event("coordinator_start", loop=args.loop)
    try:
        idle = 0
        while True:
            outcomes = run_once()
            for outcome in outcomes:
                print(json.dumps(outcome), flush=True)
                log.event("coordinator_outcome", **outcome)
            log.event("coordinator_pass", outcomes=len(outcomes),
                      paused=(bridge.TASKS.parent / "PAUSE").exists())
            if not args.loop:
                log.event("coordinator_stop", reason="single_pass")
                return 0
            idle = idle + 1 if not outcomes or all(o["state"] == "waiting_policy" for o in outcomes) else 0
            if idle >= max(1, args.max_idle_polls):
                log.event("coordinator_stop", reason="idle_limit")
                return 0
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        log.event("coordinator_stop", reason="interrupted")
        return 0
    except Exception as exc:
        # State and task data are preserved; the scheduler will invoke a fresh
        # pass later and its task result makes the failure visible.
        log.event("coordinator_error", error=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
