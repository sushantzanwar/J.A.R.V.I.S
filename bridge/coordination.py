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
import review
import tasklock

STATE_FILE = "coordination_state.json"
MAX_REVIEWS_PER_PASS = 1   # each preparation runs the whole test suite; one per pass keeps passes short


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


def process_once(tasks_dir: Path, state_path: Path, submit_followup, *, prepare_review=None,
                 create_correction=None) -> list[dict]:
    """Serialize coordination passes so concurrent clients cannot race a retry.

    prepare_review(task) builds the review packet for a completed file-changing task (post-merge review);
    create_correction(parent, request) creates the single permitted review correction. Both are optional so the
    original failure-follow-up behaviour is unchanged when they are not supplied."""
    tasks_dir = Path(tasks_dir)
    lock = tasklock.acquire(tasks_dir.parent / "locks" / "coordinator.lock", owner="coordinator")
    if lock is None:
        return []
    try:
        return _process_once(tasks_dir, state_path, submit_followup, prepare_review, create_correction)
    finally:
        lock.release()


def _correction_step(tasks_dir: Path, create_correction) -> list[dict]:
    """Turn recorded changes_requested verdicts into at most ONE correction each. All limits live in
    review.plan_correction; the outcome is recorded in the review file so a later pass never repeats it."""
    bridge_dir, outcomes = tasks_dir.parent, []
    policy = autonomy.load()
    tasks = {}
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            t = json.loads(path.read_text(encoding="utf-8"))
            tasks[int(t["id"])] = t
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for packet in review.pending_corrections(bridge_dir):
        tid = packet["task_id"]
        task = tasks.get(tid)
        needs = (task or {}).get("needs", "file-changes")
        permitted = autonomy.permits("mcp-followup", needs, tasks_dir, policy)
        decision, detail = review.plan_correction(task, packet, tasks, policy, permitted)
        out = {"event_id": f"review:{tid}", "task_id": tid, "kind": "review_correction"}
        if decision == "create":
            try:
                child = create_correction(task, detail)
                if child.get("duplicate") or not child.get("id"):
                    # The bridge returned an identical open task instead of creating one: it carries no parent link
                    # or correction marker, so it must not be recorded as this review's correction.
                    msg = "an identical open task already exists, so no linked correction was created"
                    review.set_correction(bridge_dir, tid, {"state": "blocked", "detail": msg})
                    out.update(state="correction_blocked", detail=msg)
                else:
                    review.set_correction(bridge_dir, tid, {"state": "submitted", "task_id": child["id"],
                                                             "approved": bool(child.get("approved"))})
                    out.update(state="correction_submitted", followup_id=child["id"],
                               approved=bool(child.get("approved")))
            except ValueError as exc:
                if str(exc).startswith("too many open tasks"):
                    out.update(state="waiting_policy", detail=str(exc)[:300])
                else:
                    review.set_correction(bridge_dir, tid, {"state": "blocked", "detail": str(exc)[:300]})
                    out.update(state="correction_blocked", detail=str(exc)[:300])
        elif decision == "submitted":
            review.set_correction(bridge_dir, tid, {"state": "submitted", "task_id": detail["id"],
                                                     "approved": bool(detail.get("approved"))})
            out.update(state="correction_submitted", followup_id=detail["id"], approved=bool(detail.get("approved")))
        elif decision == "blocked":
            review.set_correction(bridge_dir, tid, {"state": "blocked", "detail": str(detail)[:300]})
            out.update(state="correction_blocked", detail=str(detail)[:300])
        else:  # waiting_policy: not final, retried on a later pass
            out.update(state="waiting_policy", detail=str(detail)[:300])
        outcomes.append(out)
    return outcomes


def _process_once(tasks_dir: Path, state_path: Path, submit_followup, prepare_review=None,
                  create_correction=None) -> list[dict]:
    """Consume unseen monitor events and return durable coordinator outcomes.

    ``submit_followup`` must be the existing MCP bridge follow-up operation.  It
    retains the bridge's duplicate, tier, policy and retry-limit checks.
    """
    tasks_dir, state_path = Path(tasks_dir), Path(state_path)
    state = _load(state_path)
    outcomes = []
    prepared = 0
    for event in result_monitor.events(tasks_dir, tasks_dir.parent / "monitor_state.json", limit=200):
        event_id = event["event_id"]
        old = state["events"].get(event_id, {})
        if old.get("state") in {"completed", "followup_submitted", "retry_exhausted", "ignored"}:
            continue
        if event["kind"] == "completed":
            outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "completed"}
            task = _task(tasks_dir, event["task_id"]) if prepare_review else None
            if task and review.needs_review(task):
                existing = review.load(tasks_dir.parent, task["id"])
                tries = int(old.get("tries", 0))
                done = existing and (existing.get("verdict") or existing.get("review_id") == review.review_id(task)) \
                    and not review.retryable(existing)
                if done:
                    outcome["review"] = existing.get("status")
                elif (tasks_dir.parent / "PAUSE").exists() or prepared >= MAX_REVIEWS_PER_PASS:
                    # Not consumed: a later pass prepares it once PAUSE is lifted / there is capacity.
                    outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "review_waiting",
                               "tries": tries}
                else:
                    packet = prepare_review(task) or {}
                    prepared += 1
                    tries += 1
                    status = packet.get("status", "unavailable")
                    if tries < review.MAX_PREPARE_ATTEMPTS and (review.retryable(packet) or status in review.RETRY_STATUSES):
                        # A possibly transient failure: keep the event open and try again on a later pass, a
                        # bounded number of times, instead of freezing the failure in permanently.
                        outcome = {"event_id": event_id, "task_id": event["task_id"], "state": "review_waiting",
                                   "tries": tries, "review": status}
                    else:
                        outcome["review"] = status
                        outcome["tests"] = (packet.get("tests") or {}).get("status", "not_run")
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
    if create_correction is not None:
        outcomes += _correction_step(tasks_dir, create_correction)
    return outcomes
