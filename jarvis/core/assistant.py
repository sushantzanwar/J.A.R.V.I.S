"""Offline built-in responder for Stage 1. It answers a fixed set of questions from local data.

It deliberately has no ability to run commands, touch files or approve tasks. A language model
(local Ollama, later) will replace the fallback branch in a future stage.
"""
from datetime import datetime

from jarvis.core.tasks import awaiting_approval

HELP = (
    "Offline mode: no language model is connected yet.\n"
    "I can answer: status, tasks, time, about, help.\n"
    "Task approval is human-only: run  python bridge\\admin.py approve <id>  in a terminal."
)


FALLBACK = "I can't answer that yet; no language model is connected. Type 'help' for what works offline."


def reply(text: str, status: dict, tasks: list[dict], now: datetime | None = None) -> str:
    return answer(text, status, tasks, now) or FALLBACK


def answer(text: str, status: dict, tasks: list[dict], now: datetime | None = None) -> str | None:
    """Built-in answer for the fixed offline commands, or None when a model should handle it."""
    q = text.strip().lower()
    now = now or datetime.now()
    if not q:
        return "Say something, or type 'help'."
    if "approve" in q:
        return "I can't approve tasks. Approval is a human-only step: python bridge\\admin.py approve <id>."
    if q in ("help", "?", "commands"):
        return HELP
    if "status" in q or "system" in q:
        return (f"CPU {status['cpu']:.0%}, RAM {status['ram']:.0%} "
                f"({status['ram_used_gb']:.1f} of {status['ram_total_gb']:.1f} GB), "
                f"disk {status['disk']:.0%} used ({status['disk_used_gb']:.0f} of {status['disk_total_gb']:.0f} GB).")
    if "task" in q or "queue" in q:
        waiting = awaiting_approval(tasks)
        active = sum(1 for t in tasks if t["status"] == "in-progress")
        msg = f"{len(tasks)} tasks on record, {active} in progress, {len(waiting)} awaiting your approval."
        if waiting:
            msg += " Waiting: " + ", ".join(f"#{t['id']} {t['title']}" for t in waiting[:3]) + "."
        return msg
    if "time" in q or "date" in q:
        return now.strftime("It is %H:%M on %A, %d %B %Y.")
    if "about" in q or "who are you" in q:
        return "I'm Jarvis, a native Windows assistant in early development. Stage 1 is the interface; models come next."
    return None
