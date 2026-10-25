"""Ollama provider, talking to a local Ollama server over its documented HTTP API.

Everything stays on the machine: the default host is loopback and no credentials exist to leak.
Only two endpoints are used — `GET /api/tags` for the readiness check and `POST /api/generate`
with `stream: false` for one prompt-response round-trip. HTTP is done with `urllib` from the
standard library, so the app gains no new dependency, and the transport is injected so tests
never touch the network. Jarvis never starts the server: if it is not running the provider
reports `disconnected` and the built-in offline commands keep working.
"""
import json
import os
import re
import urllib.error
import urllib.request

from jarvis.core.providers import (CONNECTED, DISCONNECTED, ERROR, ModelProvider, ProviderError,
                                   ProviderStatus)

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:2b"
CHAT_SYSTEM_PROMPT = ("You are Jarvis, a concise assistant inside a desktop app. "
                      "You have no tools; answer from your own knowledge in a few sentences.")
THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
MAX_ERROR = 300


def http_json(url: str, payload: dict | None = None, timeout: float = 120) -> dict:
    """GET (payload None) or POST JSON and decode the JSON answer. Raises on any failure."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                 headers={"Content-Type": "application/json"})
    # no proxy handler: a loopback call must never be sent through a configured HTTP proxy
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def http_error_detail(e: urllib.error.HTTPError) -> str:
    """The server's `error` message from a failed call, or "" if the body is unreadable."""
    try:
        return str(json.loads(e.read().decode("utf-8", "replace")).get("error", ""))
    except Exception:
        return ""


class OllamaProvider(ModelProvider):
    name = "ollama"

    def __init__(self, host: str | None = None, model: str | None = None, timeout: int = 180,
                 status_timeout: int = 5, think: bool = False, transport=http_json):
        self._host = (host or os.environ.get("JARVIS_OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
        self._model = model or os.environ.get("JARVIS_OLLAMA_MODEL") or DEFAULT_MODEL
        self._timeout = timeout
        self._status_timeout = status_timeout
        # Thinking models otherwise spend hundreds of tokens reasoning before a one-line chat answer
        # (measured: 4.4s vs 0.14s on qwen3.5:2b). Set JARVIS_OLLAMA_THINK=1 to keep it on.
        self._think = think or os.environ.get("JARVIS_OLLAMA_THINK", "") == "1"
        self._http = transport

    @property
    def model(self) -> str:
        return self._model

    def installed_models(self) -> list[str]:
        """Model names the server reports. Raises; callers decide how to present a failure."""
        data = self._http(f"{self._host}/api/tags", None, self._status_timeout)
        models = data.get("models")
        if not isinstance(models, list):
            return []
        return [str(m.get("name", "")) for m in models if isinstance(m, dict) and m.get("name")]

    def _post(self, payload: dict) -> dict:
        return self._http(f"{self._host}/api/generate", payload, self._timeout)

    def status(self) -> ProviderStatus:
        try:
            names = self.installed_models()
        except urllib.error.URLError as e:   # includes connection refused and DNS failures
            reason = getattr(e, "reason", e)
            return ProviderStatus(DISCONNECTED,
                                  f"Ollama not reachable at {self._host} ({reason}); start it with: ollama serve")
        except Exception as e:              # never raise from status()
            return ProviderStatus(ERROR, f"could not query Ollama: {type(e).__name__}")
        if not names:
            return ProviderStatus(DISCONNECTED, f"Ollama is running at {self._host} but has no models installed")
        if self._model not in names:
            return ProviderStatus(DISCONNECTED,
                                  f"model '{self._model}' is not installed (ollama pull {self._model})")
        return ProviderStatus(CONNECTED, f"Ollama at {self._host}, model {self._model}; runs locally")

    # The agent planner asks for structured output; other providers ignore this.
    supports_json = True

    def generate(self, prompt: str, *, system: str | None = None, json_mode: bool = False) -> str:
        """Chat answer by default. The agent planner passes its own system prompt and
        json_mode=True, which makes Ollama constrain the reply to valid JSON."""
        payload = {"model": self._model, "prompt": prompt,
                   "system": CHAT_SYSTEM_PROMPT if system is None else system, "stream": False}
        if json_mode:
            payload["format"] = "json"
            payload["options"] = {"temperature": 0}     # plans should be repeatable, not creative
        if not self._think:
            payload["think"] = False
        try:
            data = self._post(payload)
        except urllib.error.HTTPError as e:
            detail = http_error_detail(e)
            # a model with no thinking mode rejects the field outright; ask again without it
            if "think" in detail.lower() and "think" in payload:
                retry = {k: v for k, v in payload.items() if k != "think"}
                try:
                    data = self._post(retry)
                except Exception as e2:
                    raise ProviderError(f"Ollama request failed: {type(e2).__name__}"[:MAX_ERROR]) from None
            else:
                raise ProviderError(("Ollama request failed: " + (detail or f"HTTP {e.code}"))[:MAX_ERROR]) from None
        except urllib.error.URLError as e:
            raise ProviderError(f"Ollama not reachable at {self._host}: {getattr(e, 'reason', e)}") from None
        except TimeoutError:
            raise ProviderError(f"Ollama did not answer within {self._timeout}s") from None
        except Exception as e:
            raise ProviderError(f"could not talk to Ollama: {type(e).__name__}") from None
        if data.get("error"):
            raise ProviderError(("Ollama error: " + str(data["error"]).strip())[:MAX_ERROR])
        # thinking models may inline their reasoning; the chat UI only wants the answer
        answer = THINK_BLOCK.sub("", str(data.get("response", ""))).strip()
        if not answer:
            raise ProviderError(f"model '{self._model}' returned an empty answer")
        return answer
