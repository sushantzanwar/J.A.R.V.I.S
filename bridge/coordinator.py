"""Run the persistent local result coordinator without starting a worker.

Use one pass from an active Codex workflow, or ``--loop`` in a supervised local
session. This process cannot wake an inactive Codex chat.
"""
import argparse
import json
import time

import bridge
import coordination
import mcp_server


def run_once():
    return coordination.process_once(
        bridge.TASKS, bridge.TASKS.parent / coordination.STATE_FILE,
        lambda request: mcp_server.call("submit_followup", request),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="poll until interrupted")
    parser.add_argument("--interval", type=int, default=30, help="seconds between --loop polls")
    parser.add_argument("--max-idle-polls", type=int, default=20,
                        help="stop --loop after this many idle or paused polls (default: 20)")
    args = parser.parse_args()
    try:
        idle = 0
        while True:
            outcomes = run_once()
            for outcome in outcomes:
                print(json.dumps(outcome), flush=True)
            if not args.loop:
                return 0
            idle = idle + 1 if not outcomes or all(o["state"] == "waiting_policy" for o in outcomes) else 0
            if idle >= max(1, args.max_idle_polls):
                return 0
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
