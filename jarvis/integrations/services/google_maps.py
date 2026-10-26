"""Google Maps Platform (Places, Routes) and Custom Search, using an API key from the vault.

Unlike Gmail or Calendar these are not tied to your account, so there is no consent screen: Google
issues an API key. Store it once, privately (it is never typed into chat):

    .venv\\Scripts\\python.exe scripts\\jarvis_secret.py set google_api_key
    .venv\\Scripts\\python.exe scripts\\jarvis_secret.py set search_cx        (Custom Search engine id)

Enable "Places API (New)" and "Routes API" for maps. Custom Search JSON API is closed to new
customers, so it may be refused for your project; the error is reported as it is.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from jarvis.core.agent.tools import Tool
from jarvis.core.security.vault import Vault, VaultError
from jarvis.integrations.services.base import Service

KEY, CX = "google_api_key", "search_cx"
MAX_RESULTS = 5
NO_KEY = ("Could not use Google Maps: there is no API key yet. Store it with: "
          ".venv\\Scripts\\python.exe scripts\\jarvis_secret.py set google_api_key")
NO_CX = ("Could not search the web: Custom Search needs a search engine id. Store it with: "
         ".venv\\Scripts\\python.exe scripts\\jarvis_secret.py set search_cx")
MODES = {"driving": "DRIVE", "walking": "WALK", "cycling": "BICYCLE", "transit": "TRANSIT"}


def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 20) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code} {detail}"[:160]) from None


class GoogleMapsService(Service):
    name = "google-maps"
    description = "Google Maps places and directions, and Google web search"

    def __init__(self, vault: Vault | None = None, *, http=http_json) -> None:
        self.vault = vault
        self._http = http

    def _secret(self, key: str):
        if self.vault is None:
            return None
        try:
            return self.vault.get(key)
        except VaultError:
            return None

    def configured(self) -> bool:
        return bool(self._secret(KEY))

    def places_search(self, query: str) -> str:
        key = self._secret(KEY)
        if not key:
            return NO_KEY
        try:
            out = self._http("POST", "https://places.googleapis.com/v1/places:searchText",
                             {"X-Goog-Api-Key": key,
                              "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.rating"},
                             {"textQuery": query, "maxResultCount": MAX_RESULTS})
        except Exception as e:
            return f"Could not search places: {str(e)[:140]}"
        places = out.get("places", [])
        if not places:
            return f"No places found for '{query}'."
        lines = [f"{p.get('displayName', {}).get('text', '?')} ({p.get('formattedAddress', 'no address')}"
                 + (f", rated {p['rating']}" if p.get("rating") else "") + ")" for p in places]
        return f"{len(lines)} place(s): " + "; ".join(lines) + "."

    def maps_directions(self, origin: str, destination: str, mode: str) -> str:
        key = self._secret(KEY)
        if not key:
            return NO_KEY
        travel = MODES.get(mode.strip().lower(), "DRIVE")
        try:
            out = self._http("POST", "https://routes.googleapis.com/directions/v2:computeRoutes",
                             {"X-Goog-Api-Key": key, "X-Goog-FieldMask": "routes.duration,routes.distanceMeters"},
                             {"origin": {"address": origin}, "destination": {"address": destination},
                              "travelMode": travel})
        except Exception as e:
            return f"Could not get directions: {str(e)[:140]}"
        routes = out.get("routes", [])
        if not routes:
            return f"Could not find a route from {origin} to {destination}."
        secs = int(re.sub(r"\D", "", str(routes[0].get("duration", "0s"))) or 0)
        km = routes[0].get("distanceMeters", 0) / 1000
        mins = max(1, round(secs / 60))
        span = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
        return f"From {origin} to {destination}: {km:.1f} km, about {span} by {mode.strip().lower() or 'driving'}."

    def web_search(self, query: str) -> str:
        key, cx = self._secret(KEY), self._secret(CX)
        if not key:
            return NO_KEY
        if not cx:
            return NO_CX
        url = ("https://www.googleapis.com/customsearch/v1?" +
               urllib.parse.urlencode({"key": key, "cx": cx, "q": query, "num": MAX_RESULTS}))
        try:
            out = self._http("GET", url, {})
        except Exception as e:
            return f"Could not search the web: {str(e)[:140]}"
        items = out.get("items", [])
        if not items:
            return f"No web results for '{query}'."
        return f"{len(items)} result(s): " + "; ".join(f"{i.get('title', '?')} ({i.get('link', '')})" for i in items) + "."

    def tools(self) -> list[Tool]:
        return [
            Tool("places_search", "Find places such as restaurants or shops, e.g. 'coffee shops in Pune'.",
                 self.places_search, {"query": "what and where"}, kind="query"),
            Tool("maps_directions", "Distance and travel time between two places.", self.maps_directions,
                 {"origin": "start", "destination": "end", "mode": "driving, walking, cycling or transit"}, kind="query"),
            Tool("web_search", "Search the web with Google and list the top results.", self.web_search,
                 {"query": "search text"}, kind="query"),
        ]

    _DIR = re.compile(r"^(?:directions|route)\s+from\s+(.+?)\s+to\s+(.+)$"
                      r"|^how\s+(?:far|long)\s+(?:is\s+it\s+)?from\s+(.+?)\s+to\s+(.+)$")
    _PLACES = re.compile(r"^(?:find|search\s+for|look\s+up|show\s+me)\s+(.+?)\s+(?:near|around)\s+(.+)$")
    _WEB = re.compile(r"^(?:search\s+the\s+web\s+for|web\s+search(?:\s+for)?|google\s+search\s+for)\s+(.+)$")

    def resolve(self, text: str):
        q = text.strip().lower().rstrip(" .!?,;")
        m = self._DIR.match(q)
        if m:
            o, d = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            return "maps_directions", {"origin": o.strip(), "destination": d.strip(), "mode": "driving"}
        m = self._PLACES.match(q)
        if m:
            return "places_search", {"query": f"{m.group(1).strip()} near {m.group(2).strip()}"}
        m = self._WEB.match(q)
        if m:
            return "web_search", {"query": m.group(1).strip()}
        return None
