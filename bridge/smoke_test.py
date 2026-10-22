r"""End-to-end smoke test of the MCP server over stdio, using a temp task dir.

Run:  python -I bridge\smoke_test.py
Proves the server speaks MCP and that the task lifecycle and approval gate work.
It does NOT prove ChatGPT/Codex is connected; that needs the manual step in BRIDGE_SETUP.md.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
def snapshot(d):
    return {p.name: p.read_bytes() for p in sorted(d.glob("*.json"))} if d.exists() else {}


REAL_BEFORE = snapshot(HERE / "tasks")
tmp = tempfile.mkdtemp(prefix="jarvis_bridge_test_")
env = {**os.environ, "BRIDGE_TASKS_DIR": tmp}
srv = subprocess.Popen([sys.executable, "-I", str(HERE / "mcp_server.py")], stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
_id = 0


def rpc(method, params=None):
    global _id
    _id += 1
    srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}) + "\n")
    srv.stdin.flush()
    return json.loads(srv.stdout.readline())


def tool(name, **args):
    r = rpc("tools/call", {"name": name, "arguments": args})
    if "error" in r:
        return r
    return {"isError": r["result"]["isError"], "data": r["result"]["content"][0]["text"]}


def admin(*args):
    return subprocess.run([sys.executable, "-I", str(HERE / "admin.py"), *args], env=env, capture_output=True,
                          text=True, encoding="utf-8", input="y\n")


fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}})
check("initialize", init["result"]["serverInfo"]["name"] == "jarvis-bridge")
names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
check("allowlist is exactly 4 tools", names == {"submit_task", "get_task_status", "get_task_result", "list_tasks"})
r = tool("submit_task", title="smoke", body="api_key=abcdef123456 do nothing", needs="read-only")
check("submit_task queues unapproved", not r["isError"] and '"approved": false' in r["data"])
check("secret redacted in store", "abcdef123456" not in (Path(tmp) / "001.json").read_text())
check("unapproved task not offered to Claude Code", "No approved" in admin("next").stdout)
check("no approve/exec tool via MCP", "error" in tool("approve_task", id=1) or tool("run_command", cmd="dir").get("error"))
check("bad needs rejected", tool("submit_task", title="x", body="y", needs="root")["isError"])
check("oversized body rejected", tool("submit_task", title="x", body="a" * 9000)["isError"])
check("result hidden before done", '"result": null' in tool("get_task_result", id=1)["data"])
check("human approval", "approved=True" in admin("approve", "1").stdout)
check("approved task offered", "Task 1" in admin("next").stdout)
check("complete", "done" in admin("complete", "1", "--result", "all good").stdout)
check("status via MCP", '"status": "done"' in tool("get_task_status", id=1)["data"])
check("result via MCP", "all good" in tool("get_task_result", id=1)["data"])
real = HERE / "tasks"
check("real task store untouched", snapshot(real) == REAL_BEFORE)
srv.stdin.close()
srv.wait(timeout=5)
print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
sys.exit(1 if fails else 0)
