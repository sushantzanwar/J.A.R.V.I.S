# Development status

Rolling handoff note. Update at meaningful milestones (end of a stage, a verified batch
of commits, a newly discovered blocker) — not on every commit.

**Last updated:** 2026-10-25 · **Branch:** `main` · **Working tree:** clean

## Current stage

Post-handoff consolidation. Stages A (PC control), B (voice) and C (memory/context) are
committed and green. No frontend work has started.

## Completed and verified

| Commit | Scope |
|---|---|
| `3c8b938` | Stage C: memory CRUD, FTS search, summaries, export (`store.py`, `context.py`) |
| `b32439a` | Post-merge review handoff (`bridge/review.py`, MCP tools, coordinator wiring) |
| `0c1e53f` | Stage A: Windows PC control + tests |
| `3e0e6e5` | Stage B: push-to-talk capture, SAPI recognition, orb listening state |
| `49d3187` | Wiring: voice, PC control and memory commands into app/assistant/backend |
| `85b3204` | Stage C surface: memory settings panel, diagnostics, core tests |
| `81724d1` | `run.bat` launcher |

## Verified tests

- `.venv\Scripts\python.exe -m unittest discover -s tests -t .` → **338 tests, OK**, ~80s
- `bridge/smoke_test.py` → **ALL PASSED** (13 checks)
- `tests/test_review.py` → 87 passed
- Zero skips, so no test is silently inactive on a missing optional dependency.

Reproduced independently of the Gemini audit, which reported the same 338.

## Git state

Clean tree, all stage work committed. No remote configured — nothing is pushed, and push
requires explicit approval. Task queue (`bridge/tasks/`) is deliberately untracked.

## Known issues

1. **No global push-to-talk hotkey.** Ctrl+Space is a QML `Shortcut`
   (`jarvis/ui/qml/Main.qml:27`), so it only fires while the window has focus, and it is
   a toggle rather than hold-to-talk.
2. **Voice hardware path unverified.** winmm capture and SAPI recognition are only
   mock-tested. With no mic present, `audio_recorder.start()` still returns True and
   records silence (`audio_recorder.py:57-60`); a missing speech language pack surfaces
   only as "I didn't catch that".
3. **Misleading lock fallback.** A failed `lock_workstation` reports "Workstation lock
   simulated." (`system_control.py:210`).
4. **Loose command matching.** `execute_command` uses substring matching, so a sentence
   containing "restart" or "battery" is intercepted (`system_control.py:263`).
5. **No approval path for protected actions.** `protected_action` only refuses; nothing
   can approve, so shutdown/restart are unreachable rather than gated.
6. **pytest is installed in `.venv` but undeclared** — harmless, and intentionally not
   added to `requirements-dev.txt`: the project runs on `unittest`.

## Next task

Decide between (a) a global hold-to-talk hotkey for Stage B, which needs a new dependency
and your approval, or (b) tightening `system_control` command matching and the lock
fallback, which needs no new dependency. (b) is the lower-risk default.

Frontend replacement is deferred; do not start it or remove the existing QML.
