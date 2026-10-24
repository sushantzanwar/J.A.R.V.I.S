---
name: handoff
description: Refresh docs/DEVELOPMENT_STATUS.md so a fresh session can resume J.A.R.V.I.S without re-deriving context. Use at the end of a stage, after a verified batch of commits, or when asked for a handoff or project status.
---

# Handoff

Keeps `docs/DEVELOPMENT_STATUS.md` current so a new session starts from facts instead of
re-scanning the repo.

Update these sections only, preserving the structure:

- **Last updated / branch / working tree** — from `git status --short`.
- **Current stage** — one or two sentences.
- **Completed and verified** — append new commits with `git log --oneline`. Hashes only
  for work that is committed *and* verified.
- **Verified tests** — the counts you actually observed via the `verify` skill, with the
  command. Never copy a figure forward unverified.
- **Known issues** — carry forward unresolved ones with `file:line`; delete what is fixed.
- **Next task** — exactly one recommended next step, plus anything needing approval.

Keep it under ~70 lines. It is a pointer, not documentation: link `docs/AUTOMATION.md` and
`CLAUDE.md` rather than restating them. Do not record session chatter or token counts.

Trigger points: end of a stage, a verified batch of commits, a new blocker. Not every commit.
