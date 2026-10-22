"""Read-only view of the bridge task queue. Approval and completion stay in bridge/admin.py."""
import json
import time
from pathlib import Path

DEFAULT_TASKS_DIR = Path(__file__).resolve().parents[2] / "bridge" / "tasks"
TONES = ("cyan", "ice", "violet", "muted")


def describe(t: dict, now: float | None = None) -> tuple[str, str]:
    """Accurate human label and colour tone for a task, from its real fields."""
    now = time.time() if now is None else now
    status, approved = t.get("status", "pending"), bool(t.get("approved", False))
    if status == "done":
        return "Done", "muted"
    if status == "blocked":
        return "Blocked", "violet"
    if status == "in-progress":
        who = {"worker": " (background worker)", "interactive": " (interactive session)"}.get(t.get("claimed_by"), "")
        return "In progress" + who, "ice"
    if status == "pending":
        if approved:
            if float(t.get("next_attempt_at") or 0) > now:
                return "Retry scheduled", "cyan"
            return "Approved, queued", "cyan"
        if t.get("last_error"):
            return "Failed, needs re-approval", "violet"
        return "Awaiting approval", "violet"
    return status.replace("-", " ").capitalize(), "muted"


def read_tasks(tasks_dir: Path = DEFAULT_TASKS_DIR, limit: int = 30, now: float | None = None) -> list[dict]:
    """Newest first. Malformed or unreadable files are skipped; nothing is ever written."""
    out = []
    for p in sorted(Path(tasks_dir).glob("*.json"), reverse=True)[:limit]:
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            label, tone = describe(t, now)
            out.append({
                "id": int(t["id"]),
                "title": str(t.get("title", ""))[:200],
                "status": str(t.get("status", "pending")),
                "approved": bool(t.get("approved", False)),
                "needs": str(t.get("needs", "read-only")),
                "updated": str(t.get("updated", "")),
                "label": label,
                "tone": tone,
                "attempts": int(t.get("attempts", 0) or 0),
            })
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def awaiting_approval(tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if t["status"] == "pending" and not t["approved"]]


def counts(tasks: list[dict]) -> dict:
    """Queue summary for the UI and diagnostics."""
    return {
        "awaiting": len(awaiting_approval(tasks)),
        "queued": sum(1 for t in tasks if t["status"] == "pending" and t["approved"]),
        "running": sum(1 for t in tasks if t["status"] == "in-progress"),
        "blocked": sum(1 for t in tasks if t["status"] == "blocked"),
        "done": sum(1 for t in tasks if t["status"] == "done"),
    }
