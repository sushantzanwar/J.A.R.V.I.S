"""A service is a pluggable integration (Google, WhatsApp, Android...) that contributes tools.

The agent never imports a service by name. It asks each loaded Service for its tools and for the
plain-language phrases it understands, so adding an integration means adding one file here.
"""
from __future__ import annotations

from jarvis.core.agent.tools import Tool


class Service:
    name = "service"
    description = ""

    def configured(self) -> bool:
        """True once the user has connected it (credentials present)."""
        return False

    def status(self) -> str:
        return "connected" if self.configured() else "not connected"

    def tools(self) -> list[Tool]:
        return []

    def resolve(self, text: str) -> tuple[str, dict] | None:
        """Map a plain phrase to (tool name, args) without a model, or None."""
        return None
