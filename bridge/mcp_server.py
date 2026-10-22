#!/usr/bin/env python3
"""Jarvis bridge MCP server (stdio, stdlib only, no network, no shell).

Exposes a fixed allowlist of tools to an MCP client (the OpenAI Codex/ChatGPT desktop app).
The client can submit tasks and read status/results. It CANNOT approve, execute commands,
or touch files: approval is a human-only step in admin.py, and Claude Code only runs
tasks a human has approved.
"""
import hashlib, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge  # noqa: E402  (existing task store, reused unchanged)

if os.environ.get("BRIDGE_TASKS_DIR"):
    bridge.TASKS = Path(os.environ["BRIDGE_TASKS_DIR"])

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TITLE, MAX_BODY, MAX_OPEN = 200, 8000, 20
NEEDS = ("read-only", "file-changes", "commands")


def tool(name, desc, props, required, read_only):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required,
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                            "openWorldHint": False}}


TOOLS = [
    tool("submit_task",
         "Queue a development task for Claude Code. It does NOT run until the human approves it locally.",
         {"title": {"type": "string"}, "body": {"type": "string", "description": "TASK/GOAL/CONSTRAINTS/ACCEPTANCE text"},
          "needs": {"type": "string", "enum": list(NEEDS), "description": "Highest permission the task needs"}},
         ["title", "body"], False),
    tool("get_task_status", "Status of one task (pending/in-progress/blocked/done), approval state and recent notes.",
         {"id": {"type": "integer"}}, ["id"], True),
    tool("get_task_result", "Result text of a finished task.",
         {"id": {"type": "integer"}}, ["id"], True),
    tool("list_tasks", "List all tasks with status and approval state.", {}, [], True),
]


def view(t):
    return {"id": t["id"], "title": t["title"], "status": t["status"],
            "approved": t.get("approved", False), "needs": t.get("needs", "read-only"),
            "updated": t["updated"], "notes": [n["text"] for n in t["notes"][-5:]]}


def call(name, a):
    if name == "submit_task":
        title, body = str(a.get("title", "")).strip(), str(a.get("body", "")).strip()
        needs = a.get("needs", "read-only")
        if not title or len(title) > MAX_TITLE or len(body) > MAX_BODY or needs not in NEEDS:
            raise ValueError("invalid title/body/needs")
        tasks = bridge.all_tasks()
        key = hashlib.sha256((title + "\n" + body + "\n" + needs).encode("utf-8")).hexdigest()[:16]
        dup = next((t for t in tasks if t.get("idem_key") == key and t["status"] != "done"), None)
        if dup:  # duplicate delivery of the same open task: return it instead of queuing a second copy
            return {**view(dup), "duplicate": True, "message": "Identical task already queued; no new task created."}
        if sum(1 for t in tasks if t["status"] != "done") >= MAX_OPEN:
            raise ValueError("too many open tasks; ask the human to clear the queue")
        t = {"id": max([x["id"] for x in tasks], default=0) + 1, "title": bridge.redact(title),
             "body": bridge.redact(body), "status": "pending", "created": bridge.now(),
             "updated": bridge.now(), "notes": [], "approved": False, "needs": needs,
             "source": "mcp", "idem_key": key}
        bridge.save(t)
        return {**view(t), "message": "Queued. Waiting for local human approval (python bridge\\admin.py approve %d)." % t["id"]}
    if name == "list_tasks":
        return [view(t) for t in bridge.all_tasks()]
    if name in ("get_task_status", "get_task_result"):
        p = bridge.path_for(int(a.get("id", 0)))
        if not p.exists():
            raise ValueError("no such task")
        t = json.loads(p.read_text(encoding="utf-8"))
        if name == "get_task_status":
            return view(t)
        if t["status"] != "done":
            return {"id": t["id"], "status": t["status"], "result": None}
        return {"id": t["id"], "status": "done", "result": bridge.redact(t.get("result", ""))}
    raise KeyError(name)


def handle(m):
    method, mid = m.get("method"), m.get("id")
    if mid is None:  # notification
        return None
    if method == "initialize":
        want = m.get("params", {}).get("protocolVersion")
        res = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
               "capabilities": {"tools": {}}, "serverInfo": {"name": "jarvis-bridge", "version": "0.1.0"}}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        p = m.get("params", {})
        try:
            out = call(p.get("name"), p.get("arguments") or {})
            res = {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "isError": False}
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        except Exception as e:
            res = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
