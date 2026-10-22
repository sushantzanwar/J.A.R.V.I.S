#!/usr/bin/env python3
"""Human/Claude Code side of the bridge. NOT reachable through the MCP server.

  python admin.py approve ID      # human: allow Claude Code to run this task
  python admin.py reject ID       # human: close without running
  python admin.py next            # Claude Code: print the next APPROVED pending task
  python admin.py claim ID        # Claude Code: take an approved task (atomic vs. the background worker)
  python admin.py complete ID --result TEXT   # Claude Code: record result, mark done
"""
import argparse, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge  # noqa: E402
import tasklock  # noqa: E402

if os.environ.get("BRIDGE_TASKS_DIR"):  # used by smoke_test.py
    bridge.TASKS = Path(os.environ["BRIDGE_TASKS_DIR"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("approve", "reject"):
        sub.add_parser(c).add_argument("id", type=int)
    sub.add_parser("next")
    sub.add_parser("claim").add_argument("id", type=int)
    s = sub.add_parser("complete"); s.add_argument("id", type=int); s.add_argument("--result", required=True)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if a.cmd == "next":
        for t in bridge.all_tasks():
            if t["status"] == "pending" and t.get("approved"):
                print(f"# Task {t['id']}: {t['title']}  (needs: {t.get('needs', 'read-only')})\n\n{t['body']}")
                print(f"\n(Claim it before working, so the background worker skips it: python bridge\\admin.py claim {t['id']})")
                return
        print("No approved pending tasks.")
        return
    if a.cmd == "claim":
        # same OS lock the worker takes per task: whoever gets it first owns the task
        lock = tasklock.acquire(bridge.TASKS.parent / "locks" / f"{a.id:03d}.lock", owner="interactive")
        if lock is None:
            sys.exit(f"Task {a.id} is being run by the background worker right now.")
        with lock:
            t = bridge.load(a.id)
            if t["status"] != "pending" or t.get("approved") is not True:
                sys.exit(f"Task {a.id} is {t['status']} (approved={t.get('approved', False)}); "
                         "only approved pending tasks can be claimed.")
            t["status"], t["claimed_by"] = "in-progress", "interactive"
            t["notes"].append({"at": bridge.now(), "text": "claimed by an interactive session"})
            bridge.save(t)
        print(f"Task {a.id} claimed (in-progress). Finish with: python bridge\\admin.py complete {a.id} --result \"...\"")
        return
    t = bridge.load(a.id)
    if a.cmd == "approve":
        if t["status"] != "pending":
            sys.exit(f"Task is {t['status']}, not pending")
        print(f"Task {t['id']}: {t['title']}\nNeeds: {t.get('needs', 'read-only')}\n\n{t['body']}\n")
        if input("Approve this task for Claude Code? [y/N] ").strip().lower() != "y":
            sys.exit("Not approved.")
        t["approved"] = True
        t["notes"].append({"at": bridge.now(), "text": "approved by human"})
    elif a.cmd == "reject":
        t["status"], t["approved"] = "done", False
        t["result"] = "Rejected by human."
    else:
        t["status"], t["result"] = "done", bridge.redact(a.result)
    bridge.save(t)
    print(f"Task {t['id']} -> {t['status']} (approved={t.get('approved', False)})")


if __name__ == "__main__":
    main()
