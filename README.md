# Jarvis

A personal AI assistant for Windows, built incrementally.

## Goals

- A local-first desktop assistant that runs on the owner's PC.
- Route requests to the most suitable model or tool (a Model Router is **planned**, see below).
- Keep secrets out of the repository; configuration and keys stay in local, untracked files.

## Current state

**Stage 1: native desktop shell** (PySide6 + Qt Quick/QML, not a browser or webview).
It runs fully offline and has an animated orb, live system stats, a read-only task-activity panel and a chat box
backed by a small built-in responder (`help`, `status`, `tasks`, `time`, `about`).
There is **no language model** connected yet and **no Model Router**. The app cannot run commands,
write files, or approve tasks; approval stays in `bridge/admin.py`.

| Path | Purpose |
| --- | --- |
| `jarvis/app.py`, `jarvis/__main__.py` | Entry point (`python -m jarvis`) |
| `jarvis/core/` | UI-independent logic: system stats, read-only task reader, offline responder (no Qt imports) |
| `jarvis/ui/backend.py` | The one QObject exposed to QML |
| `jarvis/ui/qml/` | The interface (Theme, Panel, StatBar, Orb, Main) |
| `tests/` | Core tests and an offscreen UI test |
| `bridge/` | Task bridge, approval controls, MCP server, smoke test, task records |

**Stage 2: provider interface and approved-task worker.** `jarvis/core/providers.py` defines a model-provider
interface with truthful connected/disconnected status. An opt-in Claude Code CLI provider
(`$env:JARVIS_PROVIDER = "claude-code"`) and `python -m jarvis.worker` (runs human-approved bridge tasks with
permission tiers) are documented, with limits, in [docs/AUTOMATION.md](docs/AUTOMATION.md).

**Stage 3: autonomous worker.** The worker can run unattended from Windows Task Scheduler
(`scripts/jarvis-scheduler.ps1`), with OS-level locks, crash recovery, bounded retries, structured logs in
`logs/worker.jsonl`, and an allowlist-only `commands` tier. Commands tasks are
scheduled only after explicit human approval and only when they match the
project command allowlist. See docs/AUTOMATION.md section 3.

The versioned `bridge/autonomy_policy.json` auto-approves trusted local MCP
read-only and file-change tasks by default. Commands-tier tasks remain
human-approved and allowlist-only. Restart the local JARVIS MCP server in
Codex Settings after updating bridge code; stdio MCP processes do not hot-reload.

**Task 9: local-first workspace and release tooling.** Persistent chat history, preferences and an activity
timeline (local SQLite, outside the repo, with corruption and migration recovery), a settings and diagnostics
screen, keyboard shortcuts and accessible labels, plus `scripts\setup.ps1`, `run-tests.ps1` and `build.ps1`.
See [docs/RELEASE.md](docs/RELEASE.md).

## Requirements

- Python 3.12 or newer
- Windows 11 (primary target)
- PySide6 (installed into a project-local `.venv`)

## Setup and launch (PowerShell)

```powershell
cd C:\J.A.R.V.I.S
python -m venv .venv                                   # once
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # once
.\.venv\Scripts\python.exe -m jarvis                   # launch the app
```

Tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
python -I bridge\smoke_test.py                          # bridge / MCP / approval gate
```

## Bridge commands

The bridge is stdlib-only and local. ChatGPT is **not** connected automatically; tasks and reports are relayed by copy-paste.

```powershell
python bridge\bridge.py add "Title" --body "text"   # or --stdin to paste a task
python bridge\bridge.py list
python bridge\bridge.py show 1
python bridge\bridge.py update 1 --status in-progress --note "progress"   # pending|in-progress|blocked|done
python bridge\bridge.py report [--file path\to\file.py]   # paste-ready status for ChatGPT
```

`report` skips secret-looking files, refuses paths outside the project, and redacts key-like strings.

## Planned architecture (not implemented)

- **Desktop UI** - framework to be decided.
- **Model Router** - selects a model/provider per request; to be designed and built.
- **Skills/tools layer** - local actions the assistant can perform, each behind explicit permission.
- **Local config/state** - stored outside version control.
