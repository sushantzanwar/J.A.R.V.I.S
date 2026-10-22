"""Model-provider interface. Pure Python: no Qt and no process spawning (concrete providers live in jarvis.integrations)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

PROVIDER_NAMES = ("offline", "claude-code")  # the only providers the app may select

CONNECTED = "connected"      # reachable and ready to answer
DISCONNECTED = "disconnected"  # not installed / not signed in / not reachable
ERROR = "error"              # a check or call failed unexpectedly
BUILTIN = "builtin"          # the built-in offline responder; no language model


class ProviderError(Exception):
    """A provider could not produce an answer. The message is safe to show to the user."""


@dataclass(frozen=True)
class ProviderStatus:
    state: str
    detail: str


class ModelProvider(ABC):
    name = "provider"

    @abstractmethod
    def status(self) -> ProviderStatus:
        """Cheap, truthful readiness check. Must never raise."""

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Return a chat answer or raise ProviderError. May block; call off the UI thread."""


    def run_task(self, prompt: str, tools: tuple[str, ...], disallowed: tuple[str, ...] = (),
                 allowed: tuple[str, ...] | None = None) -> str:
        """Run a worker task. `tools` are the tool names made available, `allowed` the permission rules that
        may run without a prompt (defaults to `tools`); everything else is denied. Providers that can't, refuse."""
        raise ProviderError(f"provider '{self.name}' cannot run tasks")

    def in_workdir(self, path):
        """Return a provider scoped to `path`; providers without process state are reusable."""
        return self


class OfflineProvider(ModelProvider):
    name = "offline"

    def status(self) -> ProviderStatus:
        return ProviderStatus(BUILTIN, "built-in responder, no language model")

    def generate(self, prompt: str) -> str:
        raise ProviderError("no language model is connected")
