"""Offline built-in responder for Stage 1. It answers a fixed set of questions from local data.

It deliberately has no ability to run commands, touch files or approve tasks. A language model
(local Ollama, later) will replace the fallback branch in a future stage.
"""
import re
from datetime import datetime

from jarvis.core.context import ContextManager
from jarvis.core.tasks import awaiting_approval

HELP = (
    "Open: open/launch/play <app, website or link> - open youtube, open notepad, open github.com/torvalds.\n"
    "Power: shutdown, restart, go to sleep, sign out, lock, cancel shutdown (shutdown waits 10 s).\n"
    "Sound: mute, volume up/down, play, pause, next track, previous track, stop music.\n"
    "Info: status, battery, uptime, active window, time, date, tasks.\n"
    "Memory: remember <fact>, what is my <key>, memories, forget <key>, archive <key>, clear memories.\n"
    "Search: search <query>, search chat <query>, summarize conversation, rebuild index.\n"
    "Chain: 'open youtube and then mute', 'mute, check the battery and then lock the pc'.\n"
    "Tasks: resume task, task status. Anything else goes to the language model.\n"
    "Task approval is human-only: run  python bridge\\admin.py approve <id>  in a terminal."
)


FALLBACK = "I can't answer that yet; no language model is connected. Type 'help' for what works offline."


def reply(text: str, status: dict, tasks: list[dict], now: datetime | None = None, system_control=None, store=None) -> str:
    return answer(text, status, tasks, now, system_control=system_control, store=store) or FALLBACK


# Anchored intents. These used to be bare substring tests, which swallowed any sentence that
# merely contained the word: "what is the solar system", "the latest update", "write me a haiku
# about winter". Now that a language model answers general questions, anything not matched here
# must fall through to it, so each pattern describes a real request about this machine.
APPROVE_RE = re.compile(r"^approve\b|\bapprove\b[^.]*\btasks?\b|\btasks?\b[^.]*\bapprove\b")
STATUS_RE = re.compile(r"^(?:system\s+)?status\b"
                       r"|^(?:what(?:'s| is)?|how(?:'s| is)?)\s+(?:the\s+)?(?:system\s+)?status\b"
                       r"|^(?:show|give)\s+(?:me\s+)?(?:the\s+)?(?:system\s+)?status\b"
                       r"|^system\s+(?:info|load|usage)\b")
TASKS_RE = re.compile(r"^tasks?\b|^(?:the\s+)?queue\b|^(?:show|list)\s+(?:me\s+)?(?:the\s+)?(?:tasks?|queue)\b"
                      r"|^(?:what|which)\s+tasks?\b|^task\s+queue\b|^how\s+many\s+tasks?\b")
TIME_RE = re.compile(r"^time$|^date$|^what\s+time\b|^what(?:'s| is)?\s+the\s+(?:time|date)\b"
                     r"|^what(?:'s| is)?\s+(?:today(?:'s)?\s+)?date\b|^what\s+day\s+is\b"
                     r"|^tell\s+me\s+the\s+(?:time|date)\b")
IDENTITY_RE = re.compile(r"^who\s+(?:are|r)\s+(?:you|u)\b|^what\s+are\s+you\b"
                         r"|^tell\s+me\s+about\s+(?:yourself|jarvis|you)\b"
                         r"|^about\s+(?:you|jarvis|this)\b|^what\s+can\s+you\s+do\b")


def answer(text: str, status: dict, tasks: list[dict], now: datetime | None = None, system_control=None, store=None) -> str | None:
    """Built-in answer for the fixed offline commands, or None when a model should handle it."""
    q = text.strip().lower()
    now = now or datetime.now()
    if not q:
        return "Say something, or type 'help'."
    if APPROVE_RE.search(q):
        return "I can't approve tasks. Approval is a human-only step: python bridge\\admin.py approve <id>."
    if q in ("help", "?", "commands"):
        return HELP
    if STATUS_RE.search(q):
        return (f"CPU {status['cpu']:.0%}, RAM {status['ram']:.0%} "
                f"({status['ram_used_gb']:.1f} of {status['ram_total_gb']:.1f} GB), "
                f"disk {status['disk']:.0%} used ({status['disk_used_gb']:.0f} of {status['disk_total_gb']:.0f} GB).")
    if TASKS_RE.search(q):
        waiting = awaiting_approval(tasks)
        active = sum(1 for t in tasks if t["status"] == "in-progress")
        msg = f"{len(tasks)} tasks on record, {active} in progress, {len(waiting)} awaiting your approval."
        if waiting:
            msg += " Waiting: " + ", ".join(f"#{t['id']} {t['title']}" for t in waiting[:3]) + "."
        return msg
    if TIME_RE.search(q):
        return now.strftime("It is %H:%M on %A, %d %B %Y.")
    if IDENTITY_RE.search(q):
        return ("I'm Jarvis, a local-first Windows assistant: offline voice, PC control and memory, "
                "with a language model for general questions.")

    # ---- memory, search and context commands
    if store is not None:
        if q in ("rebuild index", "rebuild fts", "reindex"):
            res = store.rebuild_fts()
            if res.get("status") == "ok":
                return f"Rebuilt search index: {res.get('memories_indexed', 0)} memories, {res.get('messages_indexed', 0)} messages indexed."
            return f"Failed to rebuild index: {res.get('error')}"

        if q in ("summarize conversation", "summarize chat", "summarize", "conversation summary"):
            sumry = ContextManager.summarize_conversation(store)
            if not sumry:
                return "No conversation history to summarize yet."
            return f"Summary #{sumry['id']}: {sumry['summary']}"

        if q in ("summaries", "show summaries", "list summaries"):
            sums = store.summaries(limit=5)
            if not sums:
                return "No conversation summaries recorded yet."
            lines = ["Recent Conversation Summaries:"]
            for s in sums:
                lines.append(f"• #{s['id']} ({s['ts']}): {s['summary']}")
            return "\n".join(lines)

        m_search_chat = re.match(r"^search\s+(?:chat|messages|history)\s+(.+)$", text.strip(), re.IGNORECASE)
        if m_search_chat:
            search_term = m_search_chat.group(1).strip()
            msgs = store.search_messages(search_term, limit=5)
            if not msgs:
                return f"No chat messages found matching '{search_term}'."
            lines = [f"Found {len(msgs)} chat message(s) matching '{search_term}':"]
            for m in msgs:
                lines.append(f"• [{m['role']}] {m['text']}")
            return "\n".join(lines)

        m_search_mem = re.match(r"^search\s+(?:memory|memories)\s+(.+)$", text.strip(), re.IGNORECASE)
        m_search = re.match(r"^search\s+(.+)$", text.strip(), re.IGNORECASE)
        if m_search_mem or m_search:
            search_term = (m_search_mem or m_search).group(1).strip()
            mems = store.search_memories(search_term, limit=5)
            if not mems:
                return f"No memories found matching '{search_term}'."
            lines = [f"Found {len(mems)} memory record(s) matching '{search_term}':"]
            for m in mems:
                lines.append(f"• [{m['category']}] {m['key']}: {m['value']}")
            return "\n".join(lines)

        if q in ("clear memories", "delete memories", "forget all memories", "clear all memories"):
            n = store.clear_memories(backup=True)
            return f"Cleared all memories ({n} record(s) deleted; snapshot backup created)."

        if q in ("memories", "what do you remember", "show memories", "list memories", "my memories"):
            mems = store.recall(log_usage=True)
            if not mems:
                return "I don't have any saved memories yet. Teach me with 'remember <fact>'."
            lines = ["Here is what I remember:"]
            for m in mems:
                lines.append(f"• [{m['category']}] {m['key']}: {m['value']}")
            return "\n".join(lines)

        m_archive = re.match(r"^archive\s+(?:my\s+)?([a-zA-Z0-9_\- ]+)$", text.strip(), re.IGNORECASE)
        if m_archive:
            raw_key = m_archive.group(1).strip()
            norm_key = raw_key.replace(" ", "_").lower()
            if store.archive(norm_key) or store.archive(raw_key.lower()):
                return f"Archived memory for {raw_key}."
            return f"I don't have an active memory record for '{raw_key}'."

        m_forget = re.match(r"^forget\s+(?:my\s+)?([a-zA-Z0-9_\- ]+)$", text.strip(), re.IGNORECASE)
        if m_forget:
            raw_key = m_forget.group(1).strip()
            norm_key = raw_key.replace(" ", "_").lower()
            if store.forget(norm_key) or store.forget(raw_key.lower()):
                return f"I have forgotten {raw_key}."
            return f"I don't have a memory record for '{raw_key}'."

        m_what = re.match(r"^what(?:'s|\s+is)\s+my\s+([a-zA-Z0-9_\- ]+)\??$", text.strip(), re.IGNORECASE)
        if m_what:
            raw_attr = m_what.group(1).strip()
            norm_key = raw_attr.replace(" ", "_").lower()
            mems = store.recall(key=norm_key) or store.recall(key=raw_attr.lower())
            if mems:
                return f"Your {raw_attr} is {mems[0]['value']}."
            return f"I don't remember your {raw_attr} yet. You can tell me with 'remember my {raw_attr} is ...'."

        m_rem_my = re.match(r"^remember\s+my\s+([a-zA-Z0-9_\- ]+?)\s+(?:is|=|:)\s+(.+)$", text.strip(), re.IGNORECASE)
        if m_rem_my:
            raw_attr = m_rem_my.group(1).strip()
            val = m_rem_my.group(2).strip()
            norm_key = raw_attr.replace(" ", "_").lower()
            cat = "preference" if any(w in norm_key for w in ("pref", "theme", "color", "font", "editor")) else "user_fact"
            store.remember(norm_key, val, category=cat)
            return f"I'll remember that your {raw_attr} is {val}."

        m_rem_kv = re.match(r"^remember\s+([a-zA-Z0-9_\-]+)\s*[:=]\s*(.+)$", text.strip(), re.IGNORECASE)
        if m_rem_kv:
            key = m_rem_kv.group(1).strip().lower()
            val = m_rem_kv.group(2).strip()
            cat = "preference" if any(w in key for w in ("pref", "theme", "color", "font", "editor")) else "user_fact"
            store.remember(key, val, category=cat)
            return f"Recorded in memory: {key} = {val}."

        m_rem_gen = re.match(r"^remember\s+(.+)$", text.strip(), re.IGNORECASE)
        if m_rem_gen:
            val = m_rem_gen.group(1).strip()
            key = re.sub(r"[^a-zA-Z0-9_]+", "_", val[:24]).strip("_").lower() or "note"
            store.remember(key, val, category="user_fact")
            return f"I've committed that to memory: \"{val}\"."
    else:
        if q.startswith("remember ") or q in ("memories", "what do you remember"):
            return "Memory storage is unavailable in this session."

    if system_control is not None:
        tool_result = system_control.execute_command(text)
        if tool_result is not None:
            return tool_result
    return None


def is_builtin_query(text: str) -> bool:
    """Read-only built-ins the agent may chain without a model: status, time, tasks, help."""
    q = text.strip().lower()
    return bool(STATUS_RE.search(q) or TIME_RE.search(q) or TASKS_RE.search(q) or q in ("help", "?", "commands"))
