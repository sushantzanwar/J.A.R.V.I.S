# ChatGPT/Codex desktop -> Claude Code bridge

## What this is (and is not)

- A **local stdio MCP server** (`mcp_server.py`, stdlib only). No port, no HTTP, no tunnel, no API key.
- The OpenAI desktop client (the installed `OpenAI.Codex` app) launches it as a child process.
- **ChatGPT web chat cannot use it** (it cannot reach your PC and does not read local MCP config).
- Claude Code does **not** run tasks automatically. A human approves each task, then a Claude Code
  session picks it up. That is deliberate.

## Security model

| Control | How |
| --- | --- |
| Tool allowlist | Exactly 4 tools: `submit_task`, `get_task_status`, `get_task_result`, `list_tasks`. No shell, no file access, no approve tool. |
| Approval | Only `admin.py approve ID` (interactive y/N in your terminal) lets Claude Code run a task. |
| Permissions | Each task declares `needs`: read-only / file-changes / commands. Claude Code still asks you before edits or commands. |
| Limits | Title 200 chars, body 8000, max 20 open tasks. Secret-like strings are redacted on write and read. |
| Client prompts | Config below sets `default_tools_approval_mode = "writes"`, so the client asks before `submit_task`. |

## Setup (one time, done by you)

1. Back up `%USERPROFILE%\.codex\config.toml`, then append:

```toml
[mcp_servers.jarvis_bridge]
command = "C:\\Users\\susha\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
args = ["-I", "C:\\J.A.R.V.I.S\\bridge\\mcp_server.py"]
default_tools_approval_mode = "writes"
```

   (Or: desktop app > Settings > MCP servers > Add server > STDIO, same command and args.)
2. Restart the desktop app (Settings > MCP servers > Restart).
3. Known issue: an openai/codex GitHub report (#38162) says some Windows builds list local MCP tools
   but do not expose them to Chat sessions. Test in a **Codex/agent thread** first.

## Daily flow

1. In the desktop app: "Use jarvis_bridge to submit a task: ..." -> task queued, `approved: false`.
2. You: `python bridge\admin.py approve <id>` (shows the task, asks y/N).
3. You tell Claude Code: "run the next approved bridge task" -> it runs `python bridge\admin.py next`,
   does the work (asking you for edits/commands), then `python bridge\admin.py complete <id> --result "..."`.
4. In the desktop app: ask for `get_task_status` / `get_task_result`.

## Smoke test

```powershell
python -I bridge\smoke_test.py
```

Uses a temp task directory and checks MCP handshake, allowlist, validation, redaction, the approval
gate and the full lifecycle. It does not prove the desktop app is connected; step 1 of the daily flow does.
