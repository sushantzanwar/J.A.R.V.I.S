---
name: safe-commit
description: Commit work in this shared tree safely - inspect first, group logically, stage by explicit path, never discard another agent's work. Use whenever committing J.A.R.V.I.S changes.
---

# Safe commit

This working tree is shared with other agent sessions (Gemini, Antigravity). Their
uncommitted work may be present and must survive.

1. **Inspect before touching anything:** `git status --short`, `git log --oneline -5`,
   `git diff --stat`. Check for deletions (` D`) of tracked files.
2. **Understand what you stage.** Read each diff, or classify it with a targeted grep of
   added lines. Never commit a file whose changes you have not looked at.
3. **Verify:** run the `verify` skill. Do not commit against unreproduced test claims.
4. **Group logically** — library, then wiring, then UI, then tooling. Keep unrelated work
   in separate commits. If a file's hunks genuinely interleave two features, commit it
   whole and say why in the message rather than doing hunk surgery on unverified code.
5. **Stage explicitly by path.** Never `git add -A` or `git add .`.
6. End the message with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

Never `reset --hard`, `revert`, `checkout --` or `stash` to clear someone else's changes.
There is **no remote** — never push unless explicitly asked.

State clearly in the report what you left uncommitted and why.
