# Model providers and the approved-task worker

## 1. Provider interface (`jarvis/core/providers.py`)

`ModelProvider` has `status()` (cheap, never raises) and `generate(prompt)` (may raise `ProviderError`).
Status states are reported exactly as found:

| State | Meaning |
| --- | --- |
| `builtin` | Offline responder only; no language model (the default) |
| `connected` | CLI found and signed in. Does **not** guarantee remaining plan usage |
| `disconnected` | CLI missing, signed out, or unparseable login check |
| `error` | A check or the last request failed (message shown, truncated to 300 chars) |

The app routes fixed commands (`help`, `status`, `tasks`, `time`, `about`) to the built-in responder and only
sends other questions to the provider. Provider calls run on a background thread, so the UI never blocks.

## 2. Claude Code integration: what was verified

Verified on this machine (Claude Code 2.1.306):

- `claude -p` (print mode) is the CLI's documented non-interactive interface; `--output-format json`,
  `--tools`, `--allowedTools`, `--disallowedTools` and `--permission-mode dontAsk` exist in `claude --help`.
- `claude auth status --json` reports `loggedIn: true, authMethod: claude.ai, subscriptionType: pro`, and no
  `ANTHROPIC_API_KEY` is set, so calls use the subscription login and no paid API.
- End-to-end: chat calls and worker tasks ran headlessly (see section 3, "Verified").

How it is used, and its limits:

- Jarvis only runs the first-party `claude` binary. It never reads, copies or stores login tokens, and it strips
  `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` from the child environment so an API key can never be billed.
- Every call counts against your Claude plan's usage limits and can be rate limited.
- Whether your plan's terms cover scripted use of `claude -p` is Anthropic's policy, not something this code can
  verify. Check your plan terms if you plan heavy automation.
- The app is offline by default; opt in per launch with `$env:JARVIS_PROVIDER = "claude-code"`.
- Nothing controls the interactive Claude Code / IDE UI. Every automated call is a separate `claude -p` process.

## 3. Autonomous worker (`python -m jarvis.worker`)

### 3.0 Codex autonomy policy

`bridge/autonomy_policy.json` is the version-controlled policy used when the
local Codex MCP bridge receives a task.  Its default policy auto-approves only
tasks with `source: "mcp"` and `needs: "read-only"` or `"file-changes"`.
`commands` tasks are never auto-approved.  Invalid policy files fail closed.
The existing `bridge/PAUSE` file stops both automatic approvals and worker
execution immediately.

After changing bridge Python code, restart the JARVIS MCP server in Codex
Settings before using the new behavior; an already-running stdio server does
not hot-reload source files.

### What happens automatically

Once the scheduled task is installed (section 3.5), Windows runs one worker pass at logon and every 10 minutes
while you are logged on. Each pass:

1. stops immediately if `bridge/PAUSE` exists;
2. takes the pass lock (only one pass at a time, even if started twice);
3. recovers tasks that a killed pass left `in-progress` (see "Reliability");
4. selects tasks that are `pending` **and** `approved`, skipping any still in retry backoff, at most 3 per pass;
5. checks the provider only if something is eligible, runs each task through `claude -p` with its tier's tools,
   and records the result, notes, run id and structured log events.

The worker never approves anything. Approval is `python bridge\admin.py approve <id>`, which shows the task and
asks y/N in your terminal.

### 3.1 Permission tiers (a task's `needs` is fixed at submission, shown at approval, never upgraded)

| `needs` | Claude tools | Runs from the scheduler | Automatic retry |
| --- | --- | --- | --- |
| `read-only` | Read, Glob, Grep | yes | up to 3 attempts, backoff 60 s then 120 s |
| `file-changes` | adds Edit, Write; protected paths denied | only if installed with file changes enabled | never: a failure needs re-approval |
| `commands` | adds Bash, limited to the rules in `bridge/command_allowlist.json` | **never** | never |

Always impossible for worker tasks: approving tasks; writing `bridge/`, `jarvis/worker/`, `jarvis/integrations/`,
`.git/`, `.venv/`, `.claude/`, `logs/`; any Bash command not matching an allowlisted rule. The allowlist loader
rejects rules that mention approval, the scheduler, the worker, network tools, package installs, deletion, `git
push`, elevation (`runas`, `sudo`), secrets (`.env`, token, credential, password, `.ssh`) or shell operators
(`; & | < > $`), and bare wildcards. The scheduled task runs unelevated (RunLevel Limited).

### 3.2 Commands tasks (separately authorized)

1. Copy `bridge/command_allowlist.example.json` to `bridge/command_allowlist.json` and keep only what you need.
2. Approve the task as usual.
3. Run it yourself: `.\.venv\Scripts\python.exe -m jarvis.worker --allow-commands`. The scheduler never passes
   this flag. Without a valid allowlist, commands tasks are skipped and left untouched.

### 3.3 Reliability

| Concern | Mechanism |
| --- | --- |
| Durable state | Task JSON is written to a temp file, flushed with fsync, then atomically replaced |
| Locking | OS byte-range locks (`bridge/tasklock.py`) per task and per pass. Windows releases them when a process dies, so a crash cannot leave a stale lock |
| Idempotency | Status re-checked under the lock; finished tasks never rerun; run ids (`last_run_id`, `completed_run_id`); an identical MCP submission of an open task returns the existing task (`idem_key`) |
| Timeouts | 900 s per Claude run (`--task-timeout`), at most 3 tasks per pass, scheduler stops a pass after 1 hour |
| Containment | Claude runs in a Windows Job Object: on timeout, worker crash or kill, Claude and everything it started are killed |
| Retries | Bounded and read-only only; other tiers fail closed and return to unapproved |
| Restart recovery | Next pass re-queues an interrupted read-only task (counting an attempt) and resets an interrupted file-changes or commands task to unapproved, because it may have partly applied |
| Logs | `logs/worker.jsonl` (JSON lines, redacted, rotated at 1 MB x 3); scheduler changes in `logs/scheduler-audit.jsonl` |

### 3.4 Interactive sessions and the worker

Use `admin.py next`, then **`admin.py claim <id>`**, do the work, then `admin.py complete <id> --result "..."`.
`claim` takes the same OS lock as the worker and marks the task `in-progress` (`claimed_by: interactive`), so the
worker and an interactive session can never run the same task, and recovery never touches an interactive claim.

### 3.5 Service control and logs

Installing the scheduled task is a step **you** run in your own terminal. Claude Code's permission system blocks
an agent from creating persistence (a program that starts itself at logon), which is the intended safeguard.

```powershell
# scheduled task (current user, no elevation). If scripts are blocked, prefix with:
#   powershell -NoProfile -ExecutionPolicy Bypass -File
.\scripts\jarvis-scheduler.ps1                                       # status
.\scripts\jarvis-scheduler.ps1 -Action Install -Authorize REGISTER  # one-time, explicit authorization
.\scripts\jarvis-scheduler.ps1 -Action Verify                       # run a pass now, prove it ran
.\scripts\jarvis-scheduler.ps1 -Action Disable                      # stop scheduling (Enable to resume)
.\scripts\jarvis-scheduler.ps1 -Action Uninstall                    # remove (definition backed up first)

# worker
.\.venv\Scripts\python.exe -m jarvis.worker --status                # pause state, provider, queue, last events
.\.venv\Scripts\python.exe -m jarvis.worker --pause                 # stop processing now (--resume to undo)
.\.venv\Scripts\python.exe -m jarvis.worker                         # one pass by hand, read-only tier

# logs
Get-Content logs\worker.jsonl -Tail 20
Get-Content logs\worker.jsonl -Wait -Tail 5                         # follow live
Get-Content logs\scheduler-audit.jsonl
```

The scheduler enables `--allow-file-changes --allow-commands`, but command
tasks still require explicit human approval and must match
`bridge/command_allowlist.json`.  The default allowlist covers local unit
tests, compile checks, bridge smoke tests, and read-only Git inspection; it
rejects network, installation, deletion, scheduling, approval, secret access,
and shell chaining.

### 3.6 Verified

Live runs against the real Claude Code CLI in a throwaway directory (not the project):

- queue, then execution, then completion; unapproved task ignored; second pass idle;
- two worker processes racing on one task: executed once;
- worker interpreter hard-killed mid-run: Windows released its locks, the job object killed its `claude.exe`, and
  the next pass re-queued and completed the task;
- commands tier: skipped without `--allow-commands`; with it only the allowlisted `echo hello world` ran, while a
  redirect and an `&&` injection were refused;
- pause, resume and status; duplicate MCP submission returned the same task id.

## 4. Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
python -I bridge\smoke_test.py
```

`tests/test_worker.py` covers lifecycle, duplicates, retries and backoff, crash and restart recovery, pass lock,
pause, permission tiers and the allowlist, logging and MCP idempotency. `tests/test_locks_and_containment.py`
covers lock release when a holder is killed, job-object containment and `admin.py claim`.
`tests/test_providers.py` and `tests/test_backend_provider.py` cover the provider and chat routing.
