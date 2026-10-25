"""OllamaProvider: readiness checks, one round-trip and every failure mode, all with a fake transport."""
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from jarvis.core.providers import CONNECTED, DISCONNECTED, ERROR, ProviderError
from jarvis.integrations.ollama import DEFAULT_HOST, OllamaProvider
from jarvis.integrations.registry import make_provider

HOST = "http://127.0.0.1:11434"
MODEL = "qwen3.5:2b"


def tags(*names):
    return {"models": [{"name": n} for n in names]}


def transport(answers=None, exc=None, calls=None):
    """Fake `http_json`: `answers` maps a URL suffix to the dict to return."""
    answers = answers or {}
    def http(url, payload=None, timeout=120):
        if calls is not None:
            calls.append((url, payload, timeout))
        if exc:
            raise exc
        for suffix, value in answers.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected url {url}")
    return http


def provider(**kw):
    kw.setdefault("host", HOST)
    kw.setdefault("model", MODEL)
    return OllamaProvider(**kw)


def http_error(code, body=None):
    payload = json.dumps(body or {}).encode("utf-8")
    return urllib.error.HTTPError(HOST, code, "err", {}, io.BytesIO(payload))


class StatusTests(unittest.TestCase):
    def test_connected_when_server_has_the_model(self):
        st = provider(transport=transport({"/api/tags": tags("other:1b", MODEL)})).status()
        self.assertEqual(st.state, CONNECTED)
        self.assertIn(MODEL, st.detail)

    def test_disconnected_when_server_is_down(self):
        exc = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        st = provider(transport=transport(exc=exc)).status()
        self.assertEqual(st.state, DISCONNECTED)
        self.assertIn("ollama serve", st.detail)

    def test_disconnected_when_no_models_installed(self):
        st = provider(transport=transport({"/api/tags": {"models": []}})).status()
        self.assertEqual(st.state, DISCONNECTED)
        self.assertIn("no models", st.detail)

    def test_disconnected_when_configured_model_is_missing(self):
        st = provider(transport=transport({"/api/tags": tags("something-else:7b")})).status()
        self.assertEqual(st.state, DISCONNECTED)
        self.assertIn("ollama pull", st.detail)

    def test_unexpected_failure_is_error_not_a_raise(self):
        st = provider(transport=transport(exc=ValueError("bad json"))).status()
        self.assertEqual(st.state, ERROR)
        self.assertIn("ValueError", st.detail)

    def test_status_uses_the_short_timeout(self):
        calls = []
        provider(status_timeout=3, transport=transport({"/api/tags": tags(MODEL)}, calls=calls)).status()
        self.assertEqual(calls[0][2], 3)
        self.assertIsNone(calls[0][1])  # a GET, no body


class GenerateTests(unittest.TestCase):
    def test_round_trip_returns_the_answer(self):
        calls = []
        p = provider(transport=transport({"/api/generate": {"response": "  four  "}}, calls=calls))
        self.assertEqual(p.generate("what is 2+2?"), "four")
        url, payload, _ = calls[0]
        self.assertEqual(url, f"{HOST}/api/generate")
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["prompt"], "what is 2+2?")
        self.assertFalse(payload["stream"])
        self.assertIn("Jarvis", payload["system"])

    def test_thinking_is_off_by_default_and_opt_in(self):
        calls = []
        provider(transport=transport({"/api/generate": {"response": "x"}}, calls=calls)).generate("hi")
        self.assertFalse(calls[0][1]["think"])
        calls.clear()
        provider(think=True, transport=transport({"/api/generate": {"response": "x"}}, calls=calls)).generate("hi")
        self.assertNotIn("think", calls[0][1])

    def test_think_env_override_enables_it(self):
        calls = []
        with mock.patch.dict(os.environ, {"JARVIS_OLLAMA_THINK": "1"}):
            p = provider(transport=transport({"/api/generate": {"response": "x"}}, calls=calls))
        p.generate("hi")
        self.assertNotIn("think", calls[0][1])

    def test_model_rejecting_think_is_retried_without_it(self):
        calls = []
        first = [True]
        def http(url, payload=None, timeout=120):
            calls.append((url, payload, timeout))
            if first[0]:
                first[0] = False
                raise http_error(400, {"error": '"think" is not supported by this model'})
            return {"response": "plain answer"}
        self.assertEqual(provider(transport=http).generate("hi"), "plain answer")
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0][1]["think"])
        self.assertNotIn("think", calls[1][1])

    def test_thinking_block_is_stripped(self):
        p = provider(transport=transport({"/api/generate": {"response": "<think>hmm 2+2</think>\nfour"}}))
        self.assertEqual(p.generate("hi"), "four")

    def test_empty_answer_raises(self):
        p = provider(transport=transport({"/api/generate": {"response": "   "}}))
        with self.assertRaises(ProviderError):
            p.generate("hi")

    def test_error_field_raises_with_the_server_message(self):
        p = provider(transport=transport({"/api/generate": {"error": "model not found"}}))
        with self.assertRaisesRegex(ProviderError, "model not found"):
            p.generate("hi")

    def test_http_error_body_is_surfaced(self):
        p = provider(transport=transport(exc=http_error(404, {"error": "model 'x' not found"})))
        with self.assertRaisesRegex(ProviderError, "not found"):
            p.generate("hi")

    def test_unreachable_server_raises_provider_error(self):
        p = provider(transport=transport(exc=urllib.error.URLError("refused")))
        with self.assertRaisesRegex(ProviderError, "not reachable"):
            p.generate("hi")

    def test_timeout_raises_provider_error(self):
        p = provider(timeout=7, transport=transport(exc=TimeoutError()))
        with self.assertRaisesRegex(ProviderError, "within 7s"):
            p.generate("hi")

    def test_unknown_failure_is_wrapped_not_leaked(self):
        p = provider(transport=transport(exc=ValueError("boom")))
        with self.assertRaisesRegex(ProviderError, "ValueError"):
            p.generate("hi")

    def test_cannot_run_tasks(self):
        with self.assertRaises(ProviderError):
            provider().run_task("hi", ("Read",))


class WiringTests(unittest.TestCase):
    def test_registry_builds_the_provider(self):
        self.assertEqual(make_provider("ollama").name, "ollama")

    def test_other_providers_are_preserved(self):
        self.assertEqual(make_provider("claude-code").name, "claude-code")
        self.assertEqual(make_provider("offline").name, "offline")
        self.assertEqual(make_provider("nonsense").name, "offline")

    def test_defaults(self):
        self.assertEqual(DEFAULT_HOST, "http://127.0.0.1:11434")
        self.assertEqual(OllamaProvider(transport=transport()).model, "qwen3.5:2b")

    def test_env_overrides_host_and_model(self):
        calls = []
        env = {"JARVIS_OLLAMA_HOST": "http://box:1234/", "JARVIS_OLLAMA_MODEL": "gemma4:12b"}
        with mock.patch.dict(os.environ, env):
            p = OllamaProvider(transport=transport({"/api/generate": {"response": "ok"}}, calls=calls))
        self.assertEqual(p.model, "gemma4:12b")
        p.generate("hi")
        self.assertEqual(calls[0][0], "http://box:1234/api/generate")  # trailing slash trimmed
        self.assertEqual(calls[0][1]["model"], "gemma4:12b")

    def test_selector_lists_ollama_between_offline_and_claude(self):
        from jarvis.core.providers import PROVIDER_NAMES
        self.assertEqual(PROVIDER_NAMES, ("offline", "ollama", "claude-code"))


if __name__ == "__main__":
    unittest.main()
