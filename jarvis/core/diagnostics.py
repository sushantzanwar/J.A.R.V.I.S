"""Non-destructive diagnostics summary. Built only from values the caller passes in, then redacted.

It never includes message text, task bodies, results, environment variables, tokens, or account details."""
from jarvis.core.redact import redact


def build(info: dict) -> str:
    p, s, w, q = info["provider"], info["store"], info["worker"], info["tasks"]
    lines = [
        f"Jarvis {info['version']}  |  Python {info['python']}  |  PySide6 {info['pyside']}  |  {info['platform']}",
        "",
        "Language model",
        f"  selected: {p['name']}   state: {p['state']}",
        f"  detail:   {p['detail']}",
        "",
        "Local data",
        f"  location:   {s['location']}",
        f"  persistent: {'yes' if s['persistent'] else 'NO (this session is not being saved)'}",
        f"  integrity:  {s['integrity']}   size: {s['size_kb']:.0f} KB",
        f"  messages: {s['messages']}   activity notes: {s['activity']}   memories: {s.get('memories', 0)}   summaries: {s.get('conversation_summaries', 0)}",
        f"  search index (FTS5): {s.get('memories_fts', 0)} memories, {s.get('messages_fts', 0)} messages | retention cap: {s.get('max_messages_retention', 1000)} | last latency: {s.get('last_search_latency_ms', 0.0)}ms",
    ]
    if s["notice"]:
        lines.append(f"  notice:     {s['notice']}")
    lines += [
        "",
        "Background worker (read-only view)",
        f"  state: {w['state']}   last pass: {w['last_pass'] or 'never'}   last result: {w['last_result'] or 'n/a'}",
        f"  {w['detail']}",
        "",
        "Task queue",
        f"  awaiting approval: {q['awaiting']}   queued: {q['queued']}   running: {q['running']}"
        f"   blocked: {q['blocked']}   done: {q['done']}",
        "",
        "Approval is human-only (python bridge\\admin.py approve <id>); this summary changes nothing.",
    ]
    return redact("\n".join(lines))
