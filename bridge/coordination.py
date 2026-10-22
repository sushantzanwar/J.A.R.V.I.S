"""Resumable coordinator for existing task-result events.

This module owns no queue and starts no worker.  It consumes the durable events
from :mod:`result_monitor` and, for a trusted recoverable failure, asks the
existing MCP follow-up path to create one same-tier retry.  Its own state lets a
later Codex session or local loop resume without duplicating a follow-up.
"""
import json
import os
from pathlib import Path

import autonomy
import result_monitor
import tasklock

STATE_FILE = "coordination_state.json"


def _load(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") == 1 and isinstance(state.get("events"), dict):
            return state
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": 1, "events": {}}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _task(tasks_dir: Path, task_id: int) -> dict | None:
    try:
        return json.loads((Path(tasks_dir) / f"{int(task_id):03d}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _existing_child(tasks_dir: Path, parent_id: int) -> dict | None:
    for path in sorted(Path(tasks_dir).glob("*.json")):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if task.get("parent_id") == parent_id:
            return task
    return None


def _followup_request(parent: dict, detail: str) -> dict:
    """Build a deterministic, bounded retry request; it never adds privileges."""
    attempt = int(parent.get("followup_attempt", 0)) + 1
    title = (f"Follow-up {attempt}: {parent.get('title', 'task')}")[:200]
    body = (
        f"Recoverable failure follow-up for task #{parent['id']}. Re-attempt the original task "
        f"within its existing permission tier. Preserve any existing workspace and diagnose the prior "
        f"failure before changing work.\n\nPrior failure:\n{detail}\n\nOriginal request:\n"
        f"{parent.get('body', '')}"
    )[:8000]
    return {"parent_id": parent["id"], "title": title, "body": body,
            "needs": parent.get("needs", "read-only")}


def process_once(tasks_dir: Path, state_path: Path, submit_followup) -> list[dict]:
    """Serialize coordination passes so concurrent clients cannot race a retry."""
    tasks_dir = Path(tasks_dir)
    lock = tasklock.acquire(tasks_dir.parent / "locks" / "coordinator.lock", owner="coordinator")
    if lock is None:
        return []
    try:
        return _process_once(tasks_dir, state_path, submit_followup)
    finally:
        lock.release()


def _process_once(tasks_dir: Path, state_path: Path, submit_followup) -> list[dict]:
    """Consume unseen monitor events and return durable coordinator outcomes.

    ``submit_followup`` must be the existing MCP bridge follow-up operation.  It
    retains the bridge's duplicate, tier, policy and retry-limit checks.
    """
    tasks_dir, state_path = Path(tasks_dir), Path(state_path)
    state = _load(state_path)
    outcomes = []
    for event in result_monitor.events(tasks_dir, tasks_dir.parent / "monitor_state.json", limit=200):
        event_id = event["event_id"]
        old = state["events"].get(event_id, {})
        if old.get("state") in {"completed", "followup_submitted", "retry_exhausted", "ignored"}:
            continue
        if event["kind"] == "completed":
            outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "completed"}
        else:
            parent = _task(tasks_dir, event["task_id"])
            policy = autonomy.load()
            if not parent or parent.get("source") not in ("mcp", "mcp-followup"):
                outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "ignored",
                           "detail": "not a trusted MCP task"}
            elif not autonomy.permits("mcp-followup", parent.get("needs", "read-only"), tasks_dir, policy):
                # Do not consume this event: after PAUSE is removed or policy is
                # restored, a later pass can safely continue it.
                outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "waiting_policy",
                           "detail": "automatic follow-up is paused or not permitted"}
            elif int(parent.get("followup_attempt", 0)) >= policy["max_followups"]:
                outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "retry_exhausted"}
            else:
                try:
                    child = submit_followup(_followup_request(parent, event.get("detail", "")))
                    outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "followup_submitted",
                               "followup_id": child["id"], "approved": bool(child.get("approved"))}
                except ValueError as exc:
                    # The existing bridge detects a previously-created child;
                    # this covers a crash after submission but before this
                    # coordinator wrote its state.
                    child = _existing_child(tasks_dir, parent["id"])
                    if child:
                        outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "followup_submitted",
                                   "followup_id": child["id"], "approved": bool(child.get("approved"))}
                    else:
                        # Terminal policy errors are durable stop conditions.
                        outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "retry_exhausted",
                                   "detail": str(exc)[:500]}
        state["events"][event_id] = outcome
        outcomes.append(outcome)
    _save(state_path, state)
    return outcomes
