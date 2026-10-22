"""Persistent, read-only task-result monitor for Codex polling.

It does not start workers or wake Codex.  It records each distinct completion or
recoverable worker failure once, retaining a compact event history for clients
that poll through the MCP bridge.
"""
import json
import os
from pathlib import Path

import bridge

MAX_EVENTS = 200


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("seen"), dict) and isinstance(data.get("events"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"seen": {}, "events": []}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def classify(task: dict) -> tuple[str, str] | None:
    if task.get("status") == "done":
        return "completed", bridge.redact(str(task.get("result", "")))[:500]
    # The worker preserves a recoverable failure as pending/unapproved so it
    # cannot rerun accidentally.  `last_error` is the durable failure signal.
    if task.get("status") == "pending" and not task.get("approved") and task.get("last_error"):
        return "recoverable_failure", bridge.redact(str(task["last_error"]))[:500]
    return None


def scan(tasks_dir: Path, state_path: Path) -> list[dict]:
    state = _load(state_path)
    fresh = []
    for path in sorted(Path(tasks_dir).glob("*.json")):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        outcome = classify(task)
        if not outcome:
            continue
        kind, detail = outcome
        fingerprint = f"{task['id']}:{kind}:{task.get('updated', '')}:{task.get('completed_run_id', '')}:{task.get('attempts', 0)}"
        if state["seen"].get(str(task["id"])) == fingerprint:
            continue
        event = {"event_id": fingerprint, "task_id": task["id"], "kind": kind,
                 "needs": task.get("needs", "read-only"), "source": task.get("source", ""),
                 "detail": detail, "at": task.get("updated", "")}
        state["seen"][str(task["id"])] = fingerprint
        state["events"].append(event)
        fresh.append(event)
    state["events"] = state["events"][-MAX_EVENTS:]
    _save(state_path, state)
    return fresh


def events(tasks_dir: Path, state_path: Path, limit: int = 50) -> list[dict]:
    scan(tasks_dir, state_path)
    return _load(state_path)["events"][-max(1, min(int(limit), MAX_EVENTS)):]
