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
| `e3d7a6c` | Project instructions, handoff note, workflow skills; `.claude/skills/` tracked |
| `27f23ab` | pytest declared as a development dependency |
| `aee36a3` | Instructions and status aligned with the pytest decision |
| `e804ed7` | system_control: intent-anchored command matching, honest lock failure |
| `233ccff` | Global hold-to-talk hotkey (Ctrl+Alt+Space), zero new dependencies |

## Verified tests

- `.\scripts\run-tests.ps1` (documented command) → **Ran 351 tests OK** + smoke test
  **ALL PASSED**, exit 0
- `.venv\Scripts\python.exe -m unittest discover -s tests -t .` → **351 tests, OK**, ~77s
- `.venv\Scripts\python.exe -m pytest tests/ -q` → **351 passed** (same suite, both runners agree)
- `bridge/smoke_test.py` → **ALL PASSED** (13 checks)
- `tests/test_review.py` → 87 passed
- Zero skips, so no test is silently inactive on a missing optional dependency.

Baseline was 338 before `e804ed7` added three cases; the Gemini audit's 338 was
reproduced independently at that point.

## Git state

Clean tree, all stage work committed. No remote configured — nothing is pushed, and push
requires explicit approval. Task queue (`bridge/tasks/`) is deliberately untracked.

## Known issues

1. **Global hotkey unverified on hardware.** Ctrl+Alt+Space hold-to-talk is implemented
   and unit-tested through an injected key-state function (`integrations/hotkey.py`), but
   no test presses a real key. Needs one manual check: hold it with the window unfocused.
2. **Voice hardware path unverified.** winmm capture and SAPI recognition are only
   mock-tested. With no mic present, `audio_recorder.start()` still returns True and
   records silence (`audio_recorder.py:57-60`); a missing speech language pack surfaces
   only as "I didn't catch that".
3. **No approval path for protected actions.** `protected_action` only refuses; nothing
   can approve, so shutdown/restart are unreachable rather than gated.

## Next task

Manually verify the two unverified hardware paths in one pass: launch with `run.bat`,
hold Ctrl+Alt+Space with another window focused, and confirm the orb enters `listening`,
the mic captures, and SAPI returns text. That closes known issues 1 and 2 together, or
tells us which one is broken.

Frontend replacement is deferred; do not start it or remove the existing QML.
