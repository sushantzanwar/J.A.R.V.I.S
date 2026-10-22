import json
import os
import subprocess
import unittest
from types import SimpleNamespace

from jarvis.core.providers import (BUILTIN, CONNECTED, DISCONNECTED, ERROR, OfflineProvider, ProviderError)
from jarvis.integrations import claude_cli
from jarvis.integrations.claude_cli import ClaudeCodeProvider, clean_env

EXE = r"C:\fake\claude.exe"


def runner(stdout="", returncode=0, stderr="", exc=None, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        if exc:
            raise exc
        return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)
    return run


def provider(**kw):
    return ClaudeCodeProvider(exe=EXE, runner=runner(**kw), timeout=5)


class OfflineTests(unittest.TestCase):
    def test_offline_is_truthful(self):
        p = OfflineProvider()
        self.assertEqual(p.status().state, BUILTIN)
        with self.assertRaises(ProviderError):
            p.generate("hi")
        with self.assertRaises(ProviderError):
            p.run_task("x", ("Read",))


class StatusTests(unittest.TestCase):
    def test_cli_missing_is_disconnected(self):
        orig = claude_cli.shutil.which
        claude_cli.shutil.which = lambda _: None
        try:
            self.assertEqual(ClaudeCodeProvider().status().state, DISCONNECTED)
            with self.assertRaises(ProviderError):
                ClaudeCodeProvider().generate("hi")
        finally:
            claude_cli.shutil.which = orig

    def test_logged_out_is_disconnected(self):
        self.assertEqual(provider(stdout=json.dumps({"loggedIn": False})).status().state, DISCONNECTED)

    def test_garbage_output_is_disconnected_not_connected(self):
        self.assertEqual(provider(stdout="not json").status().state, DISCONNECTED)

    def test_logged_in_is_connected_and_leaks_nothing(self):
        info = {"loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com", "orgId": "abc-123"}
        st = provider(stdout=json.dumps(info)).status()
        self.assertEqual(st.state, CONNECTED)
        self.assertNotIn("me@example.com", st.detail)
        self.assertNotIn("abc-123", st.detail)

    def test_timeout_and_crash_are_errors_not_exceptions(self):
        self.assertEqual(provider(exc=subprocess.TimeoutExpired("c", 1)).status().state, ERROR)
        self.assertEqual(provider(exc=OSError("boom")).status().state, ERROR)


class GenerateTests(unittest.TestCase):
    OK = json.dumps({"result": " hello ", "is_error": False})

    def test_success_and_command_shape(self):
        calls = []
        p = ClaudeCodeProvider(exe=EXE, runner=runner(stdout=self.OK, calls=calls))
        self.assertEqual(p.generate("what is 2+2?"), "hello")
        argv, kw = calls[0]
        self.assertEqual(argv[0], EXE)
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--tools") + 1], "")           # chat has no tools
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(kw["input"], "what is 2+2?")                     # prompt on stdin, not argv
        self.assertNotIn("what is 2+2?", argv)
        self.assertFalse(kw.get("shell", False))

    def test_tools_are_an_explicit_allowlist(self):
        calls = []
        p = ClaudeCodeProvider(exe=EXE, runner=runner(stdout=self.OK, calls=calls))
        p.run_task("t", ("Read", "Grep"), ("Edit(bridge/**)",))
        argv = calls[0][0]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Edit(bridge/**)")

    def test_failures_become_provider_errors(self):
        for kw in ({"returncode": 1, "stderr": "boom"},
                   {"stdout": json.dumps({"is_error": True, "result": "rate limited"})},
                   {"stdout": "garbage"},
                   {"exc": subprocess.TimeoutExpired("c", 1)},
                   {"exc": FileNotFoundError()}):
            with self.assertRaises(ProviderError, msg=str(kw)):
                provider(**kw).generate("hi")

    def test_error_text_is_truncated(self):
        with self.assertRaises(ProviderError) as cm:
            provider(returncode=1, stderr="x" * 5000).generate("hi")
        self.assertLess(len(str(cm.exception)), 400)

    def test_unsafe_argument_characters_refused(self):
        calls = []
        p = ClaudeCodeProvider(exe=EXE, runner=runner(stdout=self.OK, calls=calls))
        with self.assertRaises(ProviderError):
            p.run("t", ("Read",), ("Edit(a&b)",))
        self.assertEqual(calls, [])


class EnvTests(unittest.TestCase):
    def test_api_keys_and_parent_session_stripped(self):
        env = clean_env({"PATH": "p", "ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_AUTH_TOKEN": "t",
                         "CLAUDE_CODE_SESSION_ID": "1", "CLAUDECODE": "1", "KEEP": "y"})
        self.assertEqual(env, {"PATH": "p", "KEEP": "y"})

    def test_env_passed_to_child_is_clean(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        try:
            calls = []
            ClaudeCodeProvider(exe=EXE, runner=runner(stdout=GenerateTests.OK, calls=calls)).generate("hi")
            self.assertNotIn("ANTHROPIC_API_KEY", calls[0][1]["env"])
        finally:
            del os.environ["ANTHROPIC_API_KEY"]


if __name__ == "__main__":
    unittest.main()
