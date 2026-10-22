"""Single place that turns a provider name into a provider. Unknown names fall back to offline."""
from jarvis.core.providers import PROVIDER_NAMES, ModelProvider, OfflineProvider
from jarvis.integrations.claude_cli import ClaudeCodeProvider


def make_provider(name: str | None) -> ModelProvider:
    name = (name or "offline").strip().lower()
    if name == "claude-code":
        return ClaudeCodeProvider()
    return OfflineProvider()


__all__ = ["PROVIDER_NAMES", "make_provider"]
