"""Pluggable integrations. Each Service contributes tools and plain-language phrases to the agent."""
from __future__ import annotations

from jarvis.integrations.services.base import Service
from jarvis.integrations.services.google import GoogleService
from jarvis.integrations.services.google_maps import GoogleMapsService
from jarvis.integrations.services.google_workspace import GoogleWorkspaceService


def load_services(vault=None) -> list[Service]:
    """Every service Jarvis knows. A service that is not connected still loads, and reports so
    when used, so "check my email" says how to connect rather than being ignored."""
    return [GoogleService(vault), GoogleWorkspaceService(vault), GoogleMapsService(vault)]


__all__ = ["Service", "GoogleService", "GoogleWorkspaceService", "GoogleMapsService", "load_services"]
