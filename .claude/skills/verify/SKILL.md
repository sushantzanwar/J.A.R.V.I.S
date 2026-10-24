---
name: verify
description: Run the J.A.R.V.I.S test suite and bridge smoke test and report pass/fail counts. Use before committing, when asked whether the project is green, or to reproduce a test claim from another agent or session.
---

# Verify

Canonical check for this project. Tests are **unittest**-based — do not use or install pytest.

Run both, redirecting output to the scratchpad rather than into context:

```
.venv\Scripts\python.exe -m unittest discover -s tests -t . > <scratch>\ut.log 2>&1
.venv\Scripts\python.exe -I bridge\smoke_test.py > <scratch>\smoke.log 2>&1
```

Then `tail -4` each log. `.\scripts\run-tests.ps1` does both in one call and returns a
non-zero exit if either fails.

Report: the test count, OK/FAIL, and the smoke test's last line. Baseline is **338 tests,
OK** (~80s) and **ALL PASSED** (13 checks). A lower count means tests were lost — say so.

For a single area, target the file: `-m unittest tests.test_review` etc.

Rules:
- Never report a pass you did not observe. Treat another agent's or session's test claim
  as unverified until reproduced here.
- Zero skips is expected. Report any skip and its reason.
