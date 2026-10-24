"""Review packets, verdicts and the bounded correction (post-merge review).

Pure functions (analysis, capping, redaction) are tested directly; real throwaway Git repositories are used only
for the scenarios that genuinely need Git, because every Git process costs about 80 ms on Windows."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
import autonomy  # noqa: E402
import coordination  # noqa: E402
import mcp_server  # noqa: E402
import review  # noqa: E402

BASE_TEST = (
    "import unittest\n\n\nclass BaseTest(unittest.TestCase):\n"
    "    def test_ok(self):\n        self.assertEqual(1, 1)\n        self.assertTrue(True)\n"
)
POLICY = {"version": 1, "enabled": True, "trusted_sources": ["mcp", "mcp-followup"],
          "auto_approve_needs": ["read-only", "file-changes"], "max_followups": 2, "pause_file": "PAUSE"}


class Repo:
    """A real throwaway Git repository with a passing test suite."""

    def __init__(self, base: str):
        self.root = Path(base) / "repo"
        self.root.mkdir()
        self.n = 0
        self.git("init", "-q", "-b", "main")
        for rel, text in {"README.md": "hello\n", "app.py": "x = 1\n", "tests/__init__.py": "",
                          "tests/test_base.py": BASE_TEST}.items():
            self.write(rel, text)
        self.commit("base")

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.t", "-c", "core.autocrlf=false", *args],
                              cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def merge_branch(self, files, ff=False):
        self.n += 1
        name = f"work-{self.n}"
        self.git("switch", "-q", "-c", name)
        for rel, text in files.items():
            self.write(rel, text)
        checkpoint = self.commit(f"work {name}")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", *(["--ff-only"] if ff else ["--no-ff", "-m", f"merge {name}"]), name)
        return checkpoint, self.git("rev-parse", "HEAD")


def make_task(tid, checkpoint, merged, **kw):
    t = {"id": tid, "title": "Add feature", "body": "do it", "status": "done", "approved": True,
         "needs": "file-changes", "source": "mcp", "updated": "2026-10-23 10:00:00", "notes": [],
         "result": "finished", "completed_run_id": "run1",
         "workspace": {"path": "x", "branch": f"jarvis/task-{tid}", "checkpoint": checkpoint, "merged": merged}}
    t.update(kw)
    return t


def write_ready_packet(bridge_dir, task, verdict=None, notes="Please restore the removed assertion."):
    """A synthetic, ready packet (no Git needed) plus an optional recorded verdict."""
    review._atomic_write(review.packet_path(bridge_dir, task["id"]), {
        "version": 1, "task_id": task["id"], "review_id": review.review_id(task), "status": "ready",
        "workspace": {"merged": task["workspace"]["merged"], "checkpoint": task["workspace"]["checkpoint"]},
        "verdict": None, "correction": None})
    if verdict:
        review.record_verdict(bridge_dir, task["id"], verdict, notes)


def diff_of(path, added=(), removed=()):
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


class FakeTests:
    def __init__(self, status="passed", ran=3, tail="Ran 3 tests in 0.1s\n\nOK"):
        self.calls = []
        self.result = {"status": status, "ran": ran, "returncode": 0 if status == "passed" else 1,
                       "seconds": 0.1, "output_tail": tail, "command": "fake"}

    def __call__(self, path, timeout):
        self.calls.append(Path(path))
        return dict(self.result)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.bridge = self.dir / "bridge"
        (self.bridge / "tasks").mkdir(parents=True)
        self.tasks = self.bridge / "tasks"


class GitCase(Base):
    def setUp(self):
        super().setUp()
        self.repo = Repo(self.dir)

    def packet(self, files, tests=None, tid=1, **task_kw):
        cp, merged = self.repo.merge_branch(files)
        self.last = (cp, merged)
        self.runner = tests or FakeTests()
        return review.prepare(make_task(tid, cp, merged, **task_kw), self.bridge, self.repo.root, test_runner=self.runner)


# ======================================================================= pure analysis (no Git)
class AnalysisTests(Base):
    def flags(self, files, text="", withheld=(), secrets=False):
        listed = [{"status": s, "path": p} for s, p in files]
        return {f["flag"]: f for f in review.analyze(listed, list(withheld), text, secrets)}

    def test_only_completed_merged_trusted_file_changes_are_reviewable(self):
        ok = make_task(1, "a" * 40, "b" * 40)
        self.assertTrue(review.needs_review(ok))
        for bad in (make_task(1, "a" * 40, "b" * 40, needs="read-only"), make_task(1, "a" * 40, "b" * 40, status="pending"),
                    make_task(1, "a" * 40, "b" * 40, source="interactive"), make_task(1, "a" * 40, "b" * 40, source=""),
                    make_task(1, "a" * 40, "--output=evil"), make_task(1, "a" * 40, ""), make_task(1, "zz", "b" * 40),
                    make_task(1, "a" * 40, "b" * 40 + ";calc"), {**ok, "workspace": None},
                    {k: v for k, v in ok.items() if k != "workspace"}):
            self.assertFalse(review.needs_review(bad), bad.get("workspace"))
            runner = FakeTests()
            self.assertIsNone(review.prepare(bad, self.bridge, self.dir, test_runner=runner))
            self.assertEqual(runner.calls, [])
        self.assertFalse((self.bridge / "reviews").exists())

    def test_security_sensitive_paths_and_added_process_calls_are_flagged(self):
        paths = [("M", "bridge/autonomy_policy.json"), ("M", "bridge/x.py"), ("A", "scripts/run.ps1"),
                 ("M", "requirements.txt"), ("M", ".gitignore"), ("M", "jarvis/worker/w.py"), ("A", "packaging/s.spec")]
        text = diff_of("jarvis/worker/w.py", added=["import subprocess", "subprocess.run(['x'])"])
        flags = self.flags(paths, text)
        for name in ("approval-policy", "bridge-or-mcp", "scheduler-or-build-scripts", "dependencies", "ignore-rules",
                     "worker-or-provider", "process-or-network-added"):
            self.assertIn(name, flags, name)
        self.assertEqual(flags["process-or-network-added"]["files"], ["jarvis/worker/w.py"])
        self.assertEqual(flags["scheduler-or-build-scripts"]["files"], ["scripts/run.ps1", "packaging/s.spec"])

    def test_test_file_changes_are_flagged(self):
        text = diff_of("tests/test_base.py", removed=["        self.assertEqual(1, 1)", "        self.assertTrue(True)"],
                       added=["        pass", "    @unittest.skip('later')"])
        flags = self.flags([("M", "tests/test_base.py"), ("D", "tests/test_old.py")], text)
        self.assertEqual(flags["tests-changed"]["files"], ["tests/test_base.py", "tests/test_old.py"])
        self.assertEqual(flags["tests-deleted"]["files"], ["tests/test_old.py"])
        self.assertEqual(flags["assertions-removed"]["files"], ["tests/test_base.py"])
        self.assertEqual(flags["tests-skipped"]["files"], ["tests/test_base.py"])

    def test_more_assertions_added_than_removed_is_not_flagged_and_tests_are_not_process_flagged(self):
        text = diff_of("tests/test_x.py", removed=["        self.assertTrue(a)"],
                       added=["        self.assertTrue(a)", "        self.assertEqual(b, 2)", "import subprocess"])
        flags = self.flags([("M", "tests/test_x.py")], text)
        self.assertEqual(set(flags), {"tests-changed"})

    def test_clean_change_has_no_flags(self):
        self.assertEqual(self.flags([("M", "app.py"), ("A", "docs/a.md")], diff_of("app.py", added=["x = 3"])), {})

    def test_sensitive_files_and_redaction_are_flagged(self):
        flags = self.flags([("A", ".env"), ("A", "k.pem")], withheld=[".env", "k.pem"], secrets=True)
        self.assertEqual(flags["sensitive-path"]["files"], [".env", "k.pem"])
        self.assertIn("secrets-redacted", flags)

    def test_flag_file_lists_are_capped(self):
        files = [("A", f"tests/gen/t{i:03d}.py") for i in range(review.MAX_FILES + 50)]
        for f in self.flags(files).values():
            self.assertLessEqual(len(f["files"]), review.MAX_FLAG_FILES)

    def test_sensitive_path_patterns(self):
        for p in (".env", "a/.env.local", "x.key", "sub/id_rsa", "id_ed25519.pub", "db/app.sqlite3", "my_secret.txt",
                  "credentials.json", "a.pem", "d.db"):
            self.assertTrue(review.SENSITIVE.search(p), p)
        for p in ("app.py", "docs/environment.md", "tests/test_keys.py", "monkey.py"):
            self.assertFalse(review.SENSITIVE.search(p), p)

    def test_clean_strips_control_and_ansi_sequences_and_redacts(self):
        self.assertEqual(review.clean("\x1b[31mred\x1b[0m\x00ok\x07"), "redok")
        out = review.clean("api_key=abcdef123456 password: hunter22hunter sk-abcdefghijklmnopqrstuv")
        for secret in ("abcdef123456", "hunter22hunter", "sk-abcdefghijklmnopqrstuv"):
            self.assertNotIn(secret, out)
        self.assertEqual(len(review.clean("x" * 5000, 100)), 100)

    def test_cap_truncates_at_a_line_boundary_and_says_so(self):
        text = "".join(f"line {i}\n" for i in range(5000))
        out, truncated, total = review._cap(text, 1000)
        self.assertTrue(truncated)
        self.assertEqual(total, len(text.encode()))
        self.assertLessEqual(len(out), 1000 + 150)
        self.assertIn("truncated", out)
        self.assertTrue(out.split("\n[...")[0].endswith(tuple("0123456789")))     # cut after a whole line
        self.assertEqual(review._cap("short", 1000), ("short", False, 5))

    def test_additional_secret_formats_are_redacted(self):
        samples = {
            "jwt": ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                    "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk", "eyJhbGci"),
            "google": ("key = 'AIzaSyA-1234567890abcdefghijklmnopqrstuv'", "AIzaSyA"),
            "slack": ("SLACK = '" + "xoxb" + "-123456789012-1234567890123-abcdefghijklmnopqrstuvwx'", "xoxb" + "-"),   # built at runtime so secret scanners do not mistake this dummy for a real token
            "pem": ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
                    "-----END RSA PRIVATE KEY-----", "MIIEowIB"),
            "truncated pem": ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU", "b3BlbnNz"),
            "url password": ("DATABASE_URL = 'postgres://admin:S3cretPassw0rd@db.internal:5432/app'", "S3cretPassw0rd"),
            "basic": ("Authorization: Basic dXNlcjpzdXBlcnNlY3JldHBhc3N3b3Jk", "dXNlcjpz"),
            "aws secret": ("aws = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'", "wJalrXUt"),
        }
        for name, (text, secret) in samples.items():
            self.assertNotIn(secret, review.clean(text), name)
        kept = review.clean(samples["url password"][0])
        self.assertIn("postgres://", kept)                       # the structure stays readable
        self.assertIn("db.internal", kept)

    def test_redaction_leaves_ordinary_text_and_commit_ids_alone(self):
        text = ("Basic configuration options\nBearer token support was added\ncommit " + "a" * 40 +
                "\nsee http://example.com:8080/path and https://user@host/x\nself.assertEqual(a, b)")
        self.assertEqual(review.clean(text), text)

    def test_removed_test_functions_are_flagged(self):
        text = diff_of("tests/test_x.py", removed=["    def test_a(self):", "    def test_b(self):"],
                       added=["    def test_a(self):"])
        flags = self.flags([("M", "tests/test_x.py")], text)
        self.assertEqual(flags["test-functions-removed"]["files"], ["tests/test_x.py"])
        fine = diff_of("tests/test_x.py", removed=["    def test_a(self):"],
                       added=["    def test_a(self):", "    def test_c(self):"])
        self.assertNotIn("test-functions-removed", self.flags([("M", "tests/test_x.py")], fine))

    def test_incomplete_analysis_is_flagged(self):
        one = [{"status": "M", "path": "app.py"}]
        self.assertIn("diff-incomplete", {f["flag"] for f in review.analyze(one, [], "", False, incomplete=True)})
        self.assertNotIn("diff-incomplete", {f["flag"] for f in review.analyze(one, [], "", False)})

    def test_unknown_commit_gives_an_unavailable_packet_and_runs_no_tests(self):
        runner = FakeTests()
        p = review.prepare(make_task(1, "b" * 40, "a" * 40), self.bridge, self.dir, test_runner=runner)  # not a repo
        self.assertEqual(p["status"], "unavailable")
        self.assertTrue(p["warnings"])
        self.assertEqual(runner.calls, [])
        self.assertEqual((p["tests"]["status"], p["flags"], p["diff"]), ("not_run", [], None))


# ======================================================================= packets against real Git
class PacketTests(GitCase):
    def test_ready_packet_has_metadata_commits_diff_and_evidence_and_leaves_main_untouched(self):
        p = self.packet({"app.py": "x = 2\n", "new.py": "y = 1\n"})
        self.assertEqual((p["status"], p["post_merge"]), ("ready", True))
        self.assertEqual(p["workspace"], {"branch": "jarvis/task-1", "checkpoint": self.last[0], "merged": self.last[1]})
        self.assertEqual({f["path"]: f["status"] for f in p["diff"]["files"]}, {"app.py": "M", "new.py": "A"})
        self.assertIn("x = 2", p["diff"]["text"])
        self.assertIn("app.py", p["diff"]["stat"])
        self.assertEqual((p["tests"]["status"], p["tests"]["commit"]), ("passed", self.last[1]))
        self.assertIn("UNTRUSTED", p["notice"])
        self.assertIn("git revert -m 1", p["notice"])
        self.assertEqual(review.load(self.bridge, 1)["review_id"], p["review_id"])
        self.assertEqual(len(self.repo.git("worktree", "list").splitlines()), 1)      # throwaway worktree removed
        self.assertEqual((self.repo.git("status", "--porcelain"), self.repo.git("branch", "--show-current")), ("", "main"))

    def test_secrets_are_redacted_everywhere_including_test_output(self):
        tests = FakeTests(tail="leak api_key=abcdef123456 and sk-abcdefghijklmnopqrstuv")
        p = self.packet({"cfg.py": 'API_KEY = "api_key=abcdef123456"\nTOKEN = "sk-abcdefghijklmnopqrstuv"\n'},
                        tests=tests, result="done; password=hunter22hunter", title="t sk-abcdefghijklmnopqrstuv")
        raw = review.packet_path(self.bridge, 1).read_text(encoding="utf-8")
        for secret in ("abcdef123456", "hunter22hunter", "sk-abcdefghijklmnopqrstuv"):
            self.assertNotIn(secret, raw)
        self.assertIn("[REDACTED]", p["diff"]["text"])
        self.assertIn("[REDACTED]", p["tests"]["output_tail"])
        self.assertIn("secrets-redacted", [f["flag"] for f in p["flags"]])

    def test_sensitive_paths_are_listed_but_their_content_withheld(self):
        p = self.packet({".env": "SECRET_VALUE=plain-text-secret-123\n", "server.key": "-----BEGIN KEY-----\nabc\n",
                         "ok.py": "z = 1\n"})
        self.assertEqual({f["path"] for f in p["diff"]["files"]}, {".env", "server.key", "ok.py"})
        self.assertEqual(sorted(p["diff"]["withheld_sensitive"]), [".env", "server.key"])
        self.assertNotIn("plain-text-secret-123", json.dumps(p))
        self.assertNotIn("BEGIN KEY", json.dumps(p))
        self.assertIn("z = 1", p["diff"]["text"])
        self.assertIn("sensitive-path", [f["flag"] for f in p["flags"]])

    def test_diff_and_file_list_limits_hold_for_a_huge_change(self):
        files = {"big.txt": "".join(f"line {i} padding padding padding\n" for i in range(8000))}
        files.update({f"gen/f{i:03d}.txt": "x\n" for i in range(review.MAX_FILES + 30)})
        p = self.packet(files)
        self.assertTrue(p["diff"]["truncated"])
        self.assertLessEqual(len(p["diff"]["text"]), review.MAX_DIFF + 200)
        self.assertGreater(p["diff"]["total_bytes"], review.MAX_DIFF)
        self.assertEqual(p["diff"]["files_total"], review.MAX_FILES + 31)
        self.assertEqual(len(p["diff"]["files"]), review.MAX_FILES)
        self.assertLess(review.packet_path(self.bridge, 1).stat().st_size, 200_000)
        self.assertIn("diff-incomplete", [f["flag"] for f in p["flags"]])      # the reviewer is told it is partial

    def test_preparation_is_idempotent(self):
        first = self.packet({"app.py": "x = 2\n"})
        raw = review.packet_path(self.bridge, 1).read_bytes()
        second = review.prepare(make_task(1, *self.last), self.bridge, self.repo.root, test_runner=self.runner)
        self.assertEqual(len(self.runner.calls), 1)                 # the suite was run once
        self.assertEqual(second["prepared_at"], first["prepared_at"])
        self.assertEqual(review.packet_path(self.bridge, 1).read_bytes(), raw)

    def test_a_reviewed_packet_is_never_replaced(self):
        self.packet({"app.py": "x = 2\n"})
        review.record_verdict(self.bridge, 1, "approved", "looks fine")
        cp2, merged2 = self.repo.merge_branch({"app.py": "x = 9\n"})
        kept = review.prepare(make_task(1, cp2, merged2), self.bridge, self.repo.root, test_runner=FakeTests())
        self.assertEqual(kept["verdict"]["verdict"], "approved")
        self.assertEqual(kept["workspace"]["merged"], self.last[1])

    def test_checkpoint_must_be_the_second_parent_of_the_merge(self):
        cp, merged = self.repo.merge_branch({"app.py": "x = 2\n"})
        other_cp, _ = self.repo.merge_branch({"other.py": "z = 1\n"})          # an unrelated task's checkpoint
        runner = FakeTests()
        p = review.prepare(make_task(1, other_cp, merged), self.bridge, self.repo.root, test_runner=runner)
        self.assertEqual(p["status"], "unavailable")
        self.assertIn("second parent", " ".join(p["warnings"]))
        self.assertEqual((runner.calls, p["diff"], p["reviewed_commit"]), ([], None, None))
        self.assertEqual(cp, self.repo.git("rev-parse", f"{merged}^2"))        # the genuine pairing is accepted

    def test_a_plain_commit_that_is_not_the_checkpoint_is_unavailable(self):
        cp, _ = self.repo.merge_branch({"app.py": "x = 2\n"})
        self.repo.write("later.py", "q = 1\n")
        plain = self.repo.commit("a later plain commit")
        p = review.prepare(make_task(1, cp, plain), self.bridge, self.repo.root, test_runner=FakeTests())
        self.assertEqual(p["status"], "unavailable")
        self.assertIn("not a merge", " ".join(p["warnings"]))

    def test_the_reviewed_commit_is_recorded_and_pins_the_verdict(self):
        p = self.packet({"app.py": "x = 2\n"})
        self.assertEqual(p["reviewed_commit"], self.last[1])
        self.assertEqual((p["diff"]["head"], p["tests"]["commit"]), (self.last[1], self.last[1]))
        review.record_verdict(self.bridge, 1, "approved", "ok", p["review_id"])
        self.assertEqual(review.load(self.bridge, 1)["verdict"]["commit"], self.last[1])

    def test_file_names_are_sanitised_in_the_packet(self):
        p = self.packet({"api_key=abcdef123456.txt": "hello\n"})
        self.assertEqual(p["status"], "ready")
        self.assertNotIn("abcdef123456", review.packet_path(self.bridge, 1).read_text(encoding="utf-8"))

    def test_prepare_never_raises_and_stores_an_error_packet(self):
        def boom(path, timeout):
            raise RuntimeError("runner bug with api_key=abcdef123456")
        cp, merged = self.repo.merge_branch({"app.py": "x = 2\n"})
        p = review.prepare(make_task(1, cp, merged), self.bridge, self.repo.root, test_runner=boom)
        self.assertEqual(p["status"], "error")
        self.assertIn("RuntimeError", " ".join(p["warnings"]))
        raw = review.packet_path(self.bridge, 1).read_text(encoding="utf-8")
        self.assertNotIn("runner bug", raw)                                    # the message itself is never stored
        self.assertNotIn("abcdef123456", raw)
        self.assertEqual(len(self.repo.git("worktree", "list").splitlines()), 1)   # the worktree was still removed

    def test_prepare_survives_an_unwritable_review_folder(self):
        (self.bridge / "reviews").write_text("a file where the folder belongs", encoding="utf-8")
        cp, merged = self.repo.merge_branch({"app.py": "x = 2\n"})
        p = review.prepare(make_task(1, cp, merged), self.bridge, self.repo.root, test_runner=FakeTests())
        self.assertEqual(p["status"], "error")
        self.assertIn("could not be saved", " ".join(p["warnings"]))

    def test_an_unavailable_packet_is_rebuilt_when_the_cause_is_gone(self):
        cp, merged = self.repo.merge_branch({"app.py": "x = 2\n"})
        task = make_task(1, cp, merged)
        first = review.prepare(task, self.bridge, self.repo.root, git=mock.Mock(side_effect=OSError("index.lock busy")),
                               test_runner=FakeTests())
        self.assertEqual((first["status"], first["attempts"]), ("unavailable", 1))
        self.assertTrue(review.retryable(review.load(self.bridge, 1)))
        second = review.prepare(task, self.bridge, self.repo.root, test_runner=FakeTests())
        self.assertEqual((second["status"], second["attempts"]), ("ready", 2))
        self.assertFalse(review.retryable(second))
        runner = FakeTests()
        review.prepare(task, self.bridge, self.repo.root, test_runner=runner)
        self.assertEqual(runner.calls, [])                                     # a ready packet is not rebuilt

    def test_retries_are_bounded(self):
        task = make_task(1, "a" * 40, "b" * 40)                                # not in this repository: always unavailable
        for _ in range(review.MAX_PREPARE_ATTEMPTS + 2):
            p = review.prepare(task, self.bridge, self.repo.root, test_runner=FakeTests())
        self.assertEqual((p["status"], p["attempts"]), ("unavailable", review.MAX_PREPARE_ATTEMPTS))
        self.assertFalse(review.retryable(p))

    def test_missing_test_evidence_is_retried_once_the_obstacle_is_gone(self):
        cp, merged = self.repo.merge_branch({"app.py": "x = 2\n"})
        task = make_task(1, cp, merged)
        blocker = self.repo.root / ".jarvis" / "review" / f"commit-{merged[:12]}"
        blocker.mkdir(parents=True)                                            # a folder Git does not know about
        first = review.prepare(task, self.bridge, self.repo.root, test_runner=FakeTests())
        self.assertEqual(first["tests"]["status"], "not_run")
        blocker.rmdir()
        second = review.prepare(task, self.bridge, self.repo.root, test_runner=FakeTests())
        self.assertEqual((second["tests"]["status"], second["attempts"]), ("passed", 2))

    def test_fast_forward_merge_falls_back_to_the_checkpoint_diff(self):
        cp, merged = self.repo.merge_branch({"app.py": "x = 5\n"}, ff=True)
        p = review.prepare(make_task(1, cp, merged), self.bridge, self.repo.root, test_runner=FakeTests())
        self.assertEqual(p["status"], "ready")
        self.assertIn("fast-forward", " ".join(p["warnings"]))
        self.assertEqual(p["reviewed_commit"], merged)
        self.assertIn("x = 5", p["diff"]["text"])


# ======================================================================= independent test evidence
class EvidenceTests(GitCase):
    def prepared(self, files, **kw):
        cp, merged = self.repo.merge_branch(files)
        return review.prepare(make_task(1, cp, merged), self.bridge, self.repo.root, **kw)

    def test_real_run_captures_actual_results(self):
        p = self.prepared({"app.py": "x = 2\n"})
        t = p["tests"]
        self.assertEqual((t["status"], t["returncode"]), ("passed", 0))
        self.assertGreaterEqual(t["ran"], 1)
        self.assertIn("OK", t["output_tail"])
        self.assertEqual(t["commit"], p["workspace"]["merged"])
        self.assertEqual(len(self.repo.git("worktree", "list").splitlines()), 1)

    def test_real_failing_suite_is_reported_failed_never_passed(self):
        failing = BASE_TEST.replace("BaseTest", "BadTest").replace("assertEqual(1, 1)", "assertEqual(1, 2)")
        p = self.prepared({"tests/test_bad.py": failing})
        self.assertEqual(p["tests"]["status"], "failed")
        self.assertNotEqual(p["tests"]["returncode"], 0)
        self.assertIn("FAILED", p["tests"]["output_tail"])

    def stale_worktree(self, merged):
        stale = self.repo.root / ".jarvis" / "review" / f"commit-{merged[:12]}"
        stale.parent.mkdir(parents=True)
        self.repo.git("worktree", "add", "--detach", str(stale), merged)       # what a killed coordinator leaves behind
        return stale

    def test_stale_registered_clean_review_worktree_is_removed_and_tests_run(self):
        cp, merged = self.repo.merge_branch({"app.py": "x = 2\n"})
        self.stale_worktree(merged)
        runner = FakeTests()
        p = review.prepare(make_task(1, cp, merged), self.bridge, self.repo.root, test_runner=runner)
        self.assertEqual((p["tests"]["status"], len(runner.calls)), ("passed", 1))
        self.assertEqual(len(self.repo.git("worktree", "list").splitlines()), 1)

    def test_stale_registered_dirty_review_worktree_is_kept_not_forced(self):
        cp, merged = self.repo.merge_branch({"app.py": "x = 2\n"})
        stale = self.stale_worktree(merged)
        (stale / "keep.txt").write_text("local work", encoding="utf-8")
        runner = FakeTests()
        p = review.prepare(make_task(1, cp, merged), self.bridge, self.repo.root, test_runner=runner)
        self.assertEqual(p["tests"]["status"], "not_run")
        self.assertIn("local changes", p["tests"]["reason"])
        self.assertEqual(runner.calls, [])
        self.assertTrue((stale / "keep.txt").exists())

    def test_existing_review_worktree_is_not_reused_or_forced(self):
        cp, merged = self.repo.merge_branch({"app.py": "x = 2\n"})
        leftover = self.repo.root / ".jarvis" / "review" / f"commit-{merged[:12]}"
        leftover.mkdir(parents=True)
        (leftover / "keep.txt").write_text("keep", encoding="utf-8")
        runner = FakeTests()
        p = review.prepare(make_task(1, cp, merged), self.bridge, self.repo.root, test_runner=runner)
        self.assertEqual(p["tests"]["status"], "not_run")
        self.assertEqual(runner.calls, [])
        self.assertTrue((leftover / "keep.txt").exists())


class EvidenceUnitTests(Base):
    def run_with(self, **result):
        out = mock.Mock(returncode=result.get("rc", 0), stdout=result.get("out", ""), stderr=result.get("err", ""))
        with mock.patch.object(review, "run_contained", return_value=out):
            return review.unittest_runner(self.dir)

    def test_passed_requires_a_confirmed_summary(self):
        self.assertEqual(self.run_with(err="Ran 4 tests in 0.1s\n\nOK\n")["status"], "passed")
        for label, kw in (("no output", {}), ("zero tests", {"err": "Ran 0 tests in 0.0s\n\nOK\n"}),
                          ("no OK line", {"err": "Ran 4 tests in 0.1s\n"}), ("only OK", {"out": "OK"})):
            self.assertEqual(self.run_with(**kw)["status"], "inconclusive", label)
        self.assertEqual(self.run_with(rc=1, err="Ran 4 tests in 0.1s\n\nFAILED (failures=1)")["status"], "failed")

    def test_timeout_and_spawn_failure_are_recorded(self):
        with mock.patch.object(review, "run_contained", side_effect=subprocess.TimeoutExpired("x", 5)):
            self.assertEqual(review.unittest_runner(self.dir, 5)["status"], "timeout")
        with mock.patch.object(review, "run_contained", side_effect=OSError("boom")):
            self.assertEqual(review.unittest_runner(self.dir)["status"], "not_run")

    def test_test_environment_is_scrubbed_and_headless(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "CLAUDE_CODE_X": "1",
                                          "CODEX_API_KEY": "k", "KEEP_ME": "1"}):
            env = review._test_env()
        for gone in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_X", "CODEX_API_KEY"):
            self.assertNotIn(gone, env)
        self.assertEqual((env["KEEP_ME"], env["QT_QPA_PLATFORM"], env["PYTHONDONTWRITEBYTECODE"]), ("1", "offscreen", "1"))

    def test_windowless_interpreter_is_swapped_for_the_console_one(self):
        fake = self.dir / "Scripts"
        fake.mkdir()
        (fake / "pythonw.exe").write_text("")
        (fake / "python.exe").write_text("")
        with mock.patch.object(review.sys, "executable", str(fake / "pythonw.exe")):
            self.assertEqual(review._python(), str(fake / "python.exe"))

    def test_tests_run_through_the_contained_runner(self):
        out = mock.Mock(returncode=0, stdout="", stderr="Ran 1 test in 0s\n\nOK")
        with mock.patch.object(review, "run_contained", return_value=out) as rc:
            review.unittest_runner(self.dir)
        self.assertEqual(rc.call_args.kwargs["cwd"], str(self.dir))
        self.assertIn("timeout", rc.call_args.kwargs)

    def test_skipped_and_expected_failure_tests_are_not_a_clean_pass(self):
        e = self.run_with(err="Ran 190 tests in 40.0s\n\nOK (skipped=150, expected failures=2)\n")
        self.assertEqual((e["status"], e["ran"], e["skipped"], e["expected_failures"]), ("passed_with_skips", 190, 150, 2))
        clean_run = self.run_with(err="Ran 190 tests in 40.0s\n\nOK\n")
        self.assertEqual((clean_run["status"], clean_run["skipped"]), ("passed", 0))
        failed = self.run_with(rc=1, err="Ran 5 tests in 1s\n\nFAILED (failures=1, skipped=2)\n")
        self.assertEqual((failed["status"], failed["skipped"]), ("failed", 2))

    def test_caveats_are_attached_to_every_evidence_record(self):
        with mock.patch.object(review, "_git"):
            e = review.run_tests_at(self.dir, "a" * 40, None, FakeTests())
        self.assertEqual(e["caveats"], list(review.TEST_CAVEATS))

    def test_environment_scrub_covers_token_like_names(self):
        names = {"GITHUB_TOKEN": "1", "AWS_ACCESS_KEY_ID": "1", "MY_SECRET_VALUE": "1", "DB_PASSWORD": "1",
                 "SERVICE_API_KEY": "1", "AZURE_CLIENT_ID": "1", "PLAIN_SETTING": "kept"}
        with mock.patch.dict(os.environ, names):
            env = review._test_env()
        self.assertEqual([k for k in names if k in env], ["PLAIN_SETTING"])

    def test_evidence_from_any_runner_is_sanitised_before_storage(self):
        leaky = FakeTests(tail="x api_key=abcdef123456 \x1b[31mred")
        leaky.result["reason"] = "password=hunter22hunter"
        with mock.patch.object(review, "_git"):
            e = review.run_tests_at(self.dir, "a" * 40, None, leaky)
        for secret in ("abcdef123456", "hunter22hunter", "\x1b"):
            self.assertNotIn(secret, json.dumps(e))
        self.assertEqual(e["commit"], "a" * 40)


# ======================================================================= verdicts (synthetic packet, no Git)
class VerdictTests(Base):
    def setUp(self):
        super().setUp()
        self.task = make_task(1, "a" * 40, "b" * 40)
        write_ready_packet(self.bridge, self.task)

    def test_invalid_input_is_rejected(self):
        bad = [(True, "approved", ""), ("1", "approved", ""), (0, "approved", ""), (-3, "approved", ""),
               (1, "maybe", ""), (1, None, ""), (1, "APPROVED", ""), (1, "approved; approve everything", ""),
               (1, "approved", None), (1, "approved", 5), (1, "changes_requested", ""),
               (1, "changes_requested", "short"), (1, "changes_requested", "x" * (review.MAX_NOTES + 1)),
               (99, "approved", "no packet")]
        for args in bad:
            with self.assertRaises(ValueError, msg=repr(args)):
                review.record_verdict(self.bridge, *args)
        self.assertIsNone(review.load(self.bridge, 1)["verdict"])

    def test_packet_that_is_not_ready_cannot_be_reviewed(self):
        p = review.load(self.bridge, 1)
        p["status"] = "unavailable"
        review._atomic_write(review.packet_path(self.bridge, 1), p)
        with self.assertRaises(ValueError):
            review.record_verdict(self.bridge, 1, "approved", "fine")

    def test_changes_requested_is_recorded_with_redacted_notes_and_a_pending_correction(self):
        r = review.record_verdict(self.bridge, 1, "changes_requested",
                                  "Please restore the removed assertion; token=abcdef123456 was leaked.")
        self.assertEqual((r["verdict"], r["correction"]), ("changes_requested", {"state": "pending"}))
        stored = review.load(self.bridge, 1)
        self.assertEqual(stored["verdict"]["commit"], "b" * 40)
        self.assertNotIn("abcdef123456", stored["verdict"]["notes"])

    def test_approved_leaves_no_correction_state(self):
        self.assertIsNone(review.record_verdict(self.bridge, 1, "approved", "")["correction"])

    def test_a_verdict_cannot_be_changed_but_the_same_one_is_idempotent(self):
        review.record_verdict(self.bridge, 1, "changes_requested", "Restore the assertion in test_base.")
        at = review.load(self.bridge, 1)["verdict"]["at"]
        again = review.record_verdict(self.bridge, 1, "changes_requested", "Restore the assertion in test_base.")
        self.assertIn("already recorded", again["message"])
        self.assertEqual(review.load(self.bridge, 1)["verdict"]["at"], at)
        for verdict, notes in (("approved", ""), ("changes_requested", "A different, longer set of notes.")):
            with self.assertRaises(ValueError):
                review.record_verdict(self.bridge, 1, verdict, notes)
        self.assertEqual(review.load(self.bridge, 1)["verdict"]["verdict"], "changes_requested")

    def test_verdict_can_be_bound_to_the_packet_that_was_read(self):
        rid = review.load(self.bridge, 1)["review_id"]
        with self.assertRaisesRegex(ValueError, "review_id does not match"):
            review.record_verdict(self.bridge, 1, "approved", "", "1:deadbeef0000")
        with self.assertRaises(ValueError):
            review.record_verdict(self.bridge, 1, "approved", "", 5)
        self.assertIsNone(review.load(self.bridge, 1)["verdict"])
        self.assertEqual(review.record_verdict(self.bridge, 1, "approved", "", rid)["verdict"], "approved")

    def test_verdict_records_the_reviewed_commit_when_the_packet_has_one(self):
        packet = review.load(self.bridge, 1)
        packet["reviewed_commit"] = "c" * 40
        review._atomic_write(review.packet_path(self.bridge, 1), packet)
        review.record_verdict(self.bridge, 1, "approved", "")
        self.assertEqual(review.load(self.bridge, 1)["verdict"]["commit"], "c" * 40)

    def test_busy_review_is_refused_not_overwritten(self):
        lock = review._lock(self.bridge, 1)
        self.addCleanup(lock.release)
        with self.assertRaises(ValueError):
            review.record_verdict(self.bridge, 1, "approved", "")

    def test_racing_verdicts_record_exactly_one(self):
        results = []

        def go(verdict, notes):
            try:
                results.append(review.record_verdict(self.bridge, 1, verdict, notes)["verdict"])
            except ValueError:
                results.append("rejected")

        ts = [threading.Thread(target=go, args=a) for a in
              [("approved", ""), ("changes_requested", "Specific notes about the change.")] * 3]
        [t.start() for t in ts]
        [t.join() for t in ts]
        final = review.load(self.bridge, 1)["verdict"]["verdict"]
        self.assertEqual({r for r in results if r != "rejected"}, {final})


# ======================================================================= coordinator: preparation and bounded correction
class CorrectionCase(Base):
    """Coordinator behaviour with the real follow-up creation path and a hermetic policy."""

    def setUp(self):
        super().setUp()
        self.policy = dict(POLICY)
        patch = mock.patch.object(autonomy, "load", lambda path=None: dict(self.policy))
        patch.start()
        self.addCleanup(patch.stop)
        self.old_tasks = mcp_server.bridge.TASKS
        mcp_server.bridge.TASKS = self.tasks
        self.addCleanup(lambda: setattr(mcp_server.bridge, "TASKS", self.old_tasks))
        self.state = self.bridge / coordination.STATE_FILE
        self.prepared = []

    def put(self, task):
        (self.tasks / f"{task['id']:03d}.json").write_text(json.dumps(task), encoding="utf-8")
        return task

    def task(self, tid=1, **kw):
        return make_task(tid, f"{tid:040x}", f"{tid + 100:040x}", **kw)

    def ready_packet(self, task, verdict=None, notes="Please restore the removed assertion."):
        write_ready_packet(self.bridge, task, verdict, notes)

    def children(self):
        found = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.tasks.glob("*.json"))]
        return [t for t in found if t.get("parent_id")]

    def fake_prepare(self, task):
        self.prepared.append(task["id"])
        self.ready_packet(task)
        return {"status": "ready", "tests": {"status": "passed"}}

    def run_pass(self, prepare=True):
        return coordination.process_once(
            self.tasks, self.state, lambda request: mcp_server.call("submit_followup", request),
            prepare_review=self.fake_prepare if prepare else None, create_correction=mcp_server.create_review_correction)

    def correction_state(self, tid):
        return (review.load(self.bridge, tid)["correction"] or {}).get("state")


class PreparationFlowTests(CorrectionCase):
    def test_completion_prepares_one_packet_once(self):
        self.put(self.task(1))
        out = self.run_pass()
        self.assertEqual((out[0]["state"], out[0]["review"], out[0]["tests"]), ("completed", "ready", "passed"))
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.prepared, [1])

    def test_read_only_and_untrusted_tasks_get_no_packet(self):
        self.put(self.task(1, needs="read-only"))
        self.put(self.task(2, source="interactive"))
        self.run_pass()
        self.assertEqual(self.prepared, [])

    def test_pause_defers_preparation_without_consuming_the_event(self):
        self.put(self.task(1))
        (self.bridge / "PAUSE").write_text("paused", encoding="utf-8")
        self.assertEqual(self.run_pass()[0]["state"], "review_waiting")
        self.assertEqual(self.prepared, [])
        (self.bridge / "PAUSE").unlink()
        self.assertEqual(self.run_pass()[0]["review"], "ready")
        self.assertEqual(self.prepared, [1])

    def test_at_most_one_preparation_per_pass(self):
        self.put(self.task(1))
        self.put(self.task(2, title="other"))
        first = self.run_pass()
        self.assertEqual(sorted(o["state"] for o in first), ["completed", "review_waiting"])
        second = self.run_pass()
        self.assertEqual([o["state"] for o in second], ["completed"])
        self.assertEqual(sorted(self.prepared), [1, 2])

    def test_transient_preparation_failures_are_retried_then_give_up(self):
        self.put(self.task(1))
        calls = []

        def failing(task):
            calls.append(task["id"])
            return {"status": "unavailable", "attempts": len(calls)}

        def run():
            return coordination.process_once(self.tasks, self.state, lambda r: {}, prepare_review=failing)

        o1, o2, o3 = run(), run(), run()
        self.assertEqual([(o[0]["state"], o[0]["tries"]) for o in (o1, o2)], [("review_waiting", 1), ("review_waiting", 2)])
        self.assertEqual((o3[0]["state"], o3[0]["review"]), ("completed", "unavailable"))
        self.assertEqual(run(), [])
        self.assertEqual(len(calls), 3)

    def test_a_transient_failure_that_clears_ends_with_a_ready_packet(self):
        self.put(self.task(1))
        calls = []

        def flaky(task):
            calls.append(1)
            if len(calls) == 1:
                return {"status": "unavailable", "attempts": 1}
            self.ready_packet(task)
            return {"status": "ready", "attempts": 2, "tests": {"status": "passed"}}

        def run():
            return coordination.process_once(self.tasks, self.state, lambda r: {}, prepare_review=flaky)

        self.assertEqual(run()[0]["state"], "review_waiting")
        out = run()
        self.assertEqual((out[0]["state"], out[0]["review"]), ("completed", "ready"))
        self.assertEqual(run(), [])

    def test_real_prepare_with_unreachable_commits_never_crashes_a_pass_and_stops_retrying(self):
        self.put(self.task(1))                                                  # its commits exist nowhere, and this is no repo

        def run():
            return coordination.process_once(self.tasks, self.state, lambda r: {},
                                             prepare_review=lambda t: review.prepare(t, self.bridge, self.dir))
        states = [run()[0]["state"] for _ in range(3)]
        self.assertEqual(states, ["review_waiting", "review_waiting", "completed"])
        self.assertEqual(run(), [])
        self.assertEqual(review.load(self.bridge, 1)["attempts"], review.MAX_PREPARE_ATTEMPTS)

    def test_without_hooks_behaviour_is_unchanged(self):
        self.put(self.task(1))
        out = coordination.process_once(self.tasks, self.state, lambda r: {"id": 9})
        self.assertEqual(out, [{"event_id": out[0]["event_id"], "task_id": 1, "state": "completed"}])
        self.assertFalse((self.bridge / "reviews").exists())


class BoundedCorrectionTests(CorrectionCase):
    def run_pass(self, prepare=True):
        """Only the review-correction outcomes: the ordinary 'completed' event outcomes are tested elsewhere."""
        return [o for o in super().run_pass(prepare) if o.get("kind") == "review_correction"]

    def reviewed(self, tid=1, verdict="changes_requested", **kw):
        t = self.put(self.task(tid, **kw))
        self.ready_packet(t, verdict)
        return t

    def test_changes_requested_creates_exactly_one_same_tier_correction(self):
        self.reviewed()
        out = self.run_pass(prepare=False)
        self.assertEqual((out[0]["state"], out[0]["followup_id"]), ("correction_submitted", 2))
        (child,) = self.children()
        self.assertEqual((child["parent_id"], child["needs"], child["source"], child["followup_attempt"]),
                         (1, "file-changes", "mcp-followup", 1))
        self.assertTrue(child["review_correction"])
        self.assertEqual(child["review_of"], 1)
        self.assertTrue(child["approved"])                                  # existing policy decided this, not us
        self.assertIn("Please restore the removed assertion", child["body"])
        self.assertIn("ALREADY merged", child["body"])
        self.assertEqual(self.correction_state(1), "submitted")
        for _ in range(3):
            self.run_pass(prepare=False)
        self.assertEqual(len(self.children()), 1)

    def test_approved_or_missing_verdict_creates_nothing(self):
        self.reviewed(1, verdict="approved")
        self.reviewed(2, verdict=None, title="t2")
        self.assertEqual(self.run_pass(prepare=False), [])
        self.assertEqual(self.children(), [])

    def test_pause_defers_then_resumes(self):
        self.reviewed()
        (self.bridge / "PAUSE").write_text("paused", encoding="utf-8")
        self.assertEqual(self.run_pass(prepare=False)[0]["state"], "waiting_policy")
        self.assertEqual(self.children(), [])
        self.assertEqual(self.correction_state(1), "pending")               # verdict stays recorded, nothing consumed
        (self.bridge / "PAUSE").unlink()
        self.assertEqual(self.run_pass(prepare=False)[0]["state"], "correction_submitted")
        self.assertEqual(len(self.children()), 1)

    def test_policy_denial_defers_and_never_creates_an_unapproved_task(self):
        self.reviewed()
        for change in ({"enabled": False}, {"trusted_sources": ["mcp"]}, {"auto_approve_needs": ["read-only"]}):
            self.policy = {**POLICY, **change}
            self.assertEqual(self.run_pass(prepare=False)[0]["state"], "waiting_policy", change)
            self.assertEqual(self.children(), [])
        self.policy = dict(POLICY)
        self.assertEqual(self.run_pass(prepare=False)[0]["state"], "correction_submitted")

    def test_a_correction_cannot_itself_be_corrected(self):
        self.reviewed()
        self.run_pass(prepare=False)
        child = json.loads((self.tasks / "002.json").read_text(encoding="utf-8"))
        child.update(status="done", result="fixed", workspace={"path": "x", "branch": "b", "checkpoint": "c" * 40,
                                                                "merged": "d" * 40})
        self.put(child)
        self.ready_packet(child, "changes_requested", "Still wrong, please change it again.")
        out = self.run_pass(prepare=False)
        self.assertEqual(out[0]["state"], "correction_blocked")
        self.assertIn("one review correction per lineage", out[0]["detail"])
        self.assertEqual(len(self.children()), 1)                           # still only the first correction
        self.assertEqual(self.correction_state(2), "blocked")

    def test_any_descendant_of_a_correction_is_blocked(self):
        self.put(self.task(1, review_correction=True, parent_id=None))
        follow = self.put(self.task(2, parent_id=1, source="mcp-followup", followup_attempt=1))
        self.ready_packet(follow, "changes_requested", "Needs another pass over this.")
        self.assertEqual(self.run_pass(prepare=False)[0]["state"], "correction_blocked")
        self.assertEqual(len(self.children()), 1)

    def test_followup_limit_is_shared_and_respected(self):
        self.reviewed(1, followup_attempt=2, parent_id=None)
        self.assertEqual(self.run_pass(prepare=False)[0]["state"], "correction_blocked")
        self.assertEqual(self.children(), [])
        self.policy = {**POLICY, "max_followups": 5}                         # blocked is final: a new verdict is needed
        self.assertEqual(self.run_pass(prepare=False), [])
        self.assertEqual(self.correction_state(1), "blocked")

    def test_limit_of_zero_blocks_every_correction(self):
        self.policy = {**POLICY, "max_followups": 0}
        self.reviewed()
        self.assertEqual(self.run_pass(prepare=False)[0]["state"], "correction_blocked")

    def test_restart_after_creation_recovers_without_a_second_child(self):
        parent = self.reviewed()
        self.put(self.task(2, parent_id=1, source="mcp-followup", followup_attempt=1, review_correction=True,
                           review_of=1, status="pending"))
        called = []
        out = [o for o in coordination.process_once(self.tasks, self.state, lambda r: {"id": 9},
                                                    create_correction=lambda p, r: called.append(1) or {"id": 77})
               if o.get("kind") == "review_correction"]
        self.assertEqual(called, [])
        self.assertEqual((out[0]["state"], out[0]["followup_id"]), ("correction_submitted", 2))
        self.assertEqual(self.correction_state(parent["id"]), "submitted")

    def test_unrelated_existing_child_blocks(self):
        self.reviewed()
        self.put(self.task(2, parent_id=1, source="mcp-followup", followup_attempt=1, status="pending"))
        self.assertEqual(self.run_pass(prepare=False)[0]["state"], "correction_blocked")
        self.assertEqual(len(self.children()), 1)

    def test_stale_review_after_the_merge_changed_is_blocked(self):
        t = self.reviewed()
        t["workspace"]["merged"] = "e" * 40
        self.put(t)
        out = self.run_pass(prepare=False)
        self.assertEqual(out[0]["state"], "correction_blocked")
        self.assertIn("stale", out[0]["detail"])

    def test_temporary_queue_full_waits_and_other_errors_block(self):
        self.reviewed()

        def only(out):
            return [o for o in out if o.get("kind") == "review_correction"]

        def full(parent, request):
            raise ValueError("too many open tasks; ask the human to clear the queue")
        out = only(coordination.process_once(self.tasks, self.state, lambda r: {}, create_correction=full))
        self.assertEqual(out[0]["state"], "waiting_policy")
        self.assertEqual(self.correction_state(1), "pending")

        def broken(parent, request):
            raise ValueError("invalid title/body/needs")
        out = only(coordination.process_once(self.tasks, self.state, lambda r: {}, create_correction=broken))
        self.assertEqual(out[0]["state"], "correction_blocked")
        self.assertEqual(self.correction_state(1), "blocked")

    def test_full_cycle_ends_after_one_correction_however_many_passes_run(self):
        self.put(self.task(1))
        self.run_pass()                                                     # prepares the packet
        review.record_verdict(self.bridge, 1, "changes_requested", "Restore the removed assertion please.")
        self.run_pass()                                                     # creates correction #2
        child = json.loads((self.tasks / "002.json").read_text(encoding="utf-8"))
        child.update(status="done", result="fixed", completed_run_id="r2",
                     workspace={"path": "x", "branch": "b", "checkpoint": "1" * 40, "merged": "2" * 40})
        self.put(child)
        self.run_pass()                                                     # completion of #2 gets its own packet
        self.assertEqual(self.prepared, [1, 2])
        review.record_verdict(self.bridge, 2, "changes_requested", "Still not right, change it once more.")
        for _ in range(6):
            self.run_pass()
        self.assertEqual(sorted(p.name for p in self.tasks.glob("*.json")), ["001.json", "002.json"])
        self.assertEqual(self.correction_state(2), "blocked")

    def test_identical_existing_task_is_not_recorded_as_the_correction(self):
        parent = self.reviewed()
        # a trusted client pre-submits the exact task the coordinator would create
        mcp_server.call("submit_task", review.correction_request(parent, review.load(self.bridge, 1)))
        out = self.run_pass(prepare=False)
        self.assertEqual(out[0]["state"], "correction_blocked")
        self.assertIn("identical open task", out[0]["detail"])
        self.assertEqual(self.correction_state(1), "blocked")
        self.assertEqual(self.children(), [])                                   # nothing was linked as a correction

    def test_corrections_cannot_change_the_permission_tier(self):
        t = self.reviewed()
        decision, request = review.plan_correction(t, review.load(self.bridge, 1), {1: t}, dict(POLICY), True)
        self.assertEqual((decision, request["needs"]), ("create", "file-changes"))
        self.assertEqual(set(request), {"title", "body", "needs"})


# ======================================================================= MCP surface
class McpReviewTests(CorrectionCase):
    def test_tools_are_listed_without_any_approval_or_execution_tool(self):
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertTrue({"get_review_packet", "submit_review"} <= names)
        self.assertFalse([n for n in names if any(w in n for w in ("approve", "exec", "run", "shell", "correction"))])
        tools = {t["name"]: t for t in mcp_server.TOOLS}
        self.assertTrue(tools["get_review_packet"]["annotations"]["readOnlyHint"])
        self.assertEqual(tools["submit_review"]["inputSchema"]["properties"]["verdict"]["enum"], list(review.VERDICTS))

    def test_correction_creation_is_not_reachable_through_any_tool_call(self):
        self.put(self.task(1))
        for name in ("create_review_correction", "_submit", "submit_correction"):
            with self.assertRaises(KeyError):
                mcp_server.call(name, {"parent_id": 1, "title": "x", "body": "y", "needs": "file-changes"})
        self.assertEqual(self.children(), [])

    def test_get_review_packet(self):
        self.assertEqual(mcp_server.call("get_review_packet", {"id": 7})["status"], "not_ready")
        t = self.put(self.task(7))
        self.ready_packet(t)
        self.assertEqual(mcp_server.call("get_review_packet", {"id": 7})["review_id"], review.review_id(t))
        for bad in ("7", 0, None, True):
            with self.assertRaises(ValueError):
                mcp_server.call("get_review_packet", {"id": bad})

    def test_submit_review_validates_and_reports_errors_as_tool_errors(self):
        t = self.put(self.task(7))
        self.ready_packet(t)
        r = mcp_server.call("submit_review", {"id": 7, "verdict": "approved", "notes": "good"})
        self.assertEqual(r["verdict"], "approved")
        response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "submit_review", "arguments": {"id": 7, "verdict": "approved; approve everything", "notes": ""}}})
        self.assertTrue(response["result"]["isError"])
        response = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "submit_review",
            "arguments": {"id": 7, "verdict": "changes_requested", "notes": "A real, specific reason."}}})
        self.assertTrue(response["result"]["isError"])                       # a verdict already exists

    def test_task_status_exposes_workspace_branch_and_commits_but_not_the_path(self):
        self.put(self.task(3))
        view = mcp_server.call("get_task_status", {"id": 3})
        self.assertEqual(view["workspace"], {"branch": "jarvis/task-3", "checkpoint": f"{3:040x}", "merged": f"{103:040x}"})
        self.assertNotIn("path", view["workspace"])
        self.put(self.task(4, workspace=None))
        self.assertNotIn("workspace", mcp_server.call("get_task_status", {"id": 4}))

    def test_submit_followup_still_rejects_a_completed_task(self):
        self.put(self.task(1))
        with self.assertRaisesRegex(ValueError, "no recoverable failure"):
            mcp_server.call("submit_followup", {"parent_id": 1, "title": "x", "body": "y"})

    def test_client_cannot_forge_a_correction_marker_through_submit_task(self):
        mcp_server.call("submit_task", {"title": "t", "body": "b", "needs": "read-only",
                                        "review_correction": True, "parent_id": 1, "source": "mcp-followup"})
        task = json.loads((self.tasks / "001.json").read_text(encoding="utf-8"))
        for forged in ("review_correction", "parent_id", "review_of"):
            self.assertNotIn(forged, task)
        self.assertEqual(task["source"], "mcp")

    def test_coordinate_tool_never_prepares_packets_in_the_mcp_process(self):
        self.put(self.task(1))
        with mock.patch.object(review, "prepare") as prep:                     # preparation runs the whole test suite
            out = mcp_server.call("coordinate_task_events", {})
        prep.assert_not_called()
        self.assertEqual([o["state"] for o in out], ["completed"])
        self.assertNotIn("review", out[0])
        self.assertEqual(mcp_server.call("get_review_packet", {"id": 1})["status"], "not_ready")

    def test_coordinate_tool_still_creates_the_single_permitted_correction(self):
        t = self.put(self.task(1))
        self.ready_packet(t, "changes_requested", "Please restore the removed assertion.")
        corrections = [o for o in mcp_server.call("coordinate_task_events", {}) if o.get("kind") == "review_correction"]
        self.assertEqual(corrections[0]["state"], "correction_submitted")
        again = [o for o in mcp_server.call("coordinate_task_events", {}) if o.get("kind") == "review_correction"]
        self.assertEqual(again, [])
        self.assertEqual(len(self.children()), 1)

    def test_submit_review_can_bind_the_verdict_to_the_packet_read(self):
        t = self.put(self.task(7))
        self.ready_packet(t)
        with self.assertRaises(ValueError):
            mcp_server.call("submit_review", {"id": 7, "verdict": "approved", "review_id": "7:ffffffffffff"})
        r = mcp_server.call("submit_review", {"id": 7, "verdict": "approved", "notes": "",
                                              "review_id": review.review_id(t)})
        self.assertEqual(r["verdict"], "approved")
        schema = {tool["name"]: tool for tool in mcp_server.TOOLS}["submit_review"]["inputSchema"]
        self.assertIn("review_id", schema["properties"])
        self.assertNotIn("review_id", schema["required"])


if __name__ == "__main__":
    unittest.main()
