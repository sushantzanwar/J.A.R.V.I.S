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
import autonomy  # noqa: E402
import coordination  # noqa: E402
import result_monitor  # noqa: E402

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
    tool("list_task_events", "List persisted completion and recoverable-failure events for Codex polling.",
         {"limit": {"type": "integer"}}, [], True),
    tool("submit_followup", "Queue one bounded Codex follow-up for a recoverable MCP task failure.",
         {"parent_id": {"type": "integer"}, "title": {"type": "string"}, "body": {"type": "string"},
          "needs": {"type": "string", "enum": list(NEEDS)}}, ["parent_id", "title", "body"], False),
    tool("coordinate_task_events", "Process persisted task result events and submit permitted bounded follow-ups.",
         {}, [], False),
]


def view(t):
    out = {"id": t["id"], "title": t["title"], "status": t["status"],
           "approved": t.get("approved", False), "needs": t.get("needs", "read-only"),
           "updated": t["updated"], "notes": [n["text"] for n in t["notes"][-5:]]}
    if t.get("approval_origin"):
        out["approval_origin"] = t["approval_origin"]
    return out


def _submit(a, source="mcp", parent=None):
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
        auto_approved = autonomy.permits(source, needs, bridge.TASKS)
        t = {"id": max([x["id"] for x in tasks], default=0) + 1, "title": bridge.redact(title),
             "body": bridge.redact(body), "status": "pending", "created": bridge.now(),
             "updated": bridge.now(), "notes": [], "approved": auto_approved, "needs": needs,
             "source": source, "idem_key": key}
        if parent is not None:
            t["parent_id"] = parent["id"]
            t["followup_attempt"] = int(parent.get("followup_attempt", 0)) + 1
        if auto_approved:
            t["approval_origin"] = "autonomy-policy-v1"
            t["notes"].append({"at": bridge.now(), "text": "auto-approved by autonomy policy"})
        bridge.save(t)
        message = "Queued and auto-approved by autonomy policy." if auto_approved else \
            "Queued. Waiting for local human approval (python bridge\\admin.py approve %d)." % t["id"]
        return {**view(t), "message": message}


def call(name, a):
    if name == "submit_task":
        return _submit(a)
    if name == "list_task_events":
        state = bridge.TASKS.parent / "monitor_state.json"
        return result_monitor.events(bridge.TASKS, state, a.get("limit", 50))
    if name == "coordinate_task_events":
        return coordination.process_once(
            bridge.TASKS, bridge.TASKS.parent / coordination.STATE_FILE,
            lambda request: call("submit_followup", request),
        )
    if name == "submit_followup":
        parent_id = int(a.get("parent_id", 0))
        parent = next((t for t in bridge.all_tasks() if t["id"] == parent_id), None)
        if not parent:
            raise ValueError("no such parent task")
        if parent.get("source") not in ("mcp", "mcp-followup"):
            raise ValueError("parent was not submitted by the trusted MCP flow")
        event = result_monitor.classify(parent)
        if not event or event[0] != "recoverable_failure":
            raise ValueError("parent has no recoverable failure")
        policy = autonomy.load()
        if int(parent.get("followup_attempt", 0)) >= policy["max_followups"]:
            raise ValueError("follow-up retry limit reached")
        if any(t.get("parent_id") == parent_id for t in bridge.all_tasks()):
            raise ValueError("a follow-up already exists for this task")
        needs = a.get("needs", parent.get("needs", "read-only"))
        if needs != parent.get("needs", "read-only"):
            raise ValueError("follow-up cannot change its permission tier")
        return _submit({**a, "needs": needs}, source="mcp-followup", parent=parent)
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
