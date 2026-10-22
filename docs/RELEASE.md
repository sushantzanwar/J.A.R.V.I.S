# Setup, launch, test and package (Windows)

Everything below stays inside the project folder. Nothing is installed globally and no paid service is used.

## 1. One-time setup

```powershell
cd C:\J.A.R.V.I.S
.\scripts\setup.ps1          # creates .venv and installs PySide6 (needs Python 3.12 or newer)
.\scripts\setup.ps1 -Dev     # additionally installs PyInstaller, only needed to build the .exe
```

Re-running is safe. If `pip` times out on a slow network, run it again; downloads are retried.
If PowerShell blocks scripts, run them as `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1`.

## 2. Launch

```powershell
.\.venv\Scripts\python.exe -m jarvis                        # offline mode (default)
$env:JARVIS_PROVIDER = "claude-code"; .\.venv\Scripts\python.exe -m jarvis   # use your signed-in Claude Code for this launch
```

Shortcuts: `Ctrl+L` focus chat, `Ctrl+1` tasks, `Ctrl+2` timeline, `Ctrl+,` settings, `Esc` close dialogs.

## 3. Test

```powershell
.\scripts\run-tests.ps1      # full unit/UI suite plus the bridge smoke test; non-zero exit on any failure
```

Tests use temporary folders and in-memory databases. They never touch your real task queue, chat history or
settings, never start the scheduler and never approve anything.

## 4. Package a Windows app folder

```powershell
.\scripts\build.ps1          # builds dist\Jarvis\Jarvis.exe (PyInstaller, one folder, no console window)
.\scripts\build.ps1 -Smoke   # build, then launch it briefly with a throwaway data folder and check a window opens
```

- `dist\` and `build\` are ignored by Git; generated binaries are never committed.
- The package contains only the desktop UI. The bridge and worker stay in the project folder. To see the task
  queue and worker status in a packaged build, point it at the project: `$env:JARVIS_HOME = "C:\J.A.R.V.I.S"`.
- The package is not code-signed, so Windows SmartScreen may warn the first time. Signing needs a certificate,
  which is outside the free path.
- Automating the build on every change, an installer (MSI/Inno Setup) and signing are not part of this task.

## 5. Where your data lives

| What | Where |
| --- | --- |
| Chat history, preferences, activity notes | `%LOCALAPPDATA%\Jarvis\jarvis.db` (override: `JARVIS_DATA_DIR`) |
| Qt shader cache | `%LOCALAPPDATA%\Jarvis\cache` |
| Task queue and approvals | `bridge\tasks\` (not in Git) |
| Worker log | `logs\worker.jsonl` (not in Git) |

The database is never inside the repository. Secret-looking text (API keys, tokens, passwords) is redacted before
it is saved, and provider logins are never stored. Settings has a read-only diagnostics summary with no message
text, task text, keys or account details.

## 6. If local data is damaged

The app never silently discards data and tells you at the top of the window:

| Situation | What happens |
| --- | --- |
| File unreadable or corrupt | It is kept as `jarvis.db.corrupt-<time>` beside the original and a new empty history is started |
| File from a newer Jarvis | Left untouched; this session runs in memory and says so |
| Upgrade (migration) fails | A copy `jarvis.db.bak-v<N>` was made first; the file stays at its last good version |
| Folder not writable | This session runs in memory and says so |

To start completely fresh, close Jarvis and move `%LOCALAPPDATA%\Jarvis\jarvis.db` somewhere else (do not delete it
until you are sure). To uninstall, delete the project folder and `%LOCALAPPDATA%\Jarvis`.

## 7. Status honesty

Settings shows the real provider state (`builtin`, `connected`, `disconnected`, `error`) with an explanation, the
background worker state read from its pause file and log (`active`, `paused`, `stale`, `unknown`), and the task
queue counts. It offers no approve, run, or worker-control buttons; approval stays in a terminal.
