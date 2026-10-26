"""Google Workspace (Contacts, Tasks, Docs, Sheets, Slides, Forms) and Maps / web search.

Google is a MagicMock API; Maps goes through an injected HTTP function. Nothing reaches the network.
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jarvis.core.agent import Agent
from jarvis.core.security import Policy, Vault
from jarvis.integrations.services import GoogleMapsService, GoogleService, GoogleWorkspaceService, load_services
from jarvis.integrations.services.google import SCOPES, TASKS, TOKEN_KEY
from tests.test_agent import FakeControl


def vault_with(**secrets):
    d = tempfile.mkdtemp()
    v = Vault(Path(d) / "v.bin")
    for k, val in secrets.items():
        v.set(k, val)
    return v


class WorkspaceTests(unittest.TestCase):
    def ws(self):
        api = mock.MagicMock()
        return GoogleWorkspaceService(vault_with(**{TOKEN_KEY: {"token": "t", "scopes": SCOPES}}),
                                      build=lambda n, v: api, now=lambda: datetime(2026, 10, 27, 9, 0)), api

    def test_contacts(self):
        g, api = self.ws()
        api.people().searchContacts().execute.return_value = {"results": [{"person": {
            "names": [{"displayName": "Bob Rao"}], "phoneNumbers": [{"value": "+91 98765 43210"}]}}]}
        out = g.contacts_search("bob")
        self.assertIn("Bob Rao", out)
        self.assertIn("98765", out)
        api.people().searchContacts().execute.return_value = {}
        self.assertIn("No contacts found", g.contacts_search("zzz"))

    def test_tasks_list_and_add(self):
        g, api = self.ws()
        api.tasks().list().execute.return_value = {"items": [{"title": "Buy milk"}, {"title": "Call mom"}]}
        self.assertIn("Buy milk; Call mom", g.tasks_list())
        api.tasks().list().execute.return_value = {}
        self.assertIn("empty", g.tasks_list())
        api.tasks().insert().execute.return_value = {"title": "Pay rent"}
        self.assertIn("Pay rent", g.tasks_add("Pay rent"))
        self.assertIn("no title", g.tasks_add("   "))

    def test_docs_create_with_text_and_read(self):
        g, api = self.ws()
        api.documents().create().execute.return_value = {"documentId": "d1"}
        self.assertIn("Created the Google Doc 'Notes'", g.docs_create("Notes", "hello"))
        self.assertEqual(api.documents().batchUpdate.call_args.kwargs["body"]["requests"][0]["insertText"]["text"], "hello")
        api.files().list().execute.return_value = {"files": [{"id": "d1", "name": "Notes"}]}
        api.documents().get().execute.return_value = {"body": {"content": [
            {"paragraph": {"elements": [{"textRun": {"content": "Line one\n"}}, {"textRun": {"content": "Line two"}}]}}]}}
        self.assertIn("Notes: Line one Line two", g.docs_read("notes"))

    def test_docs_read_missing_name(self):
        g, api = self.ws()
        api.files().list().execute.return_value = {"files": []}
        self.assertIn("Could not find a Google doc called 'ghost'", g.docs_read("ghost"))

    def test_sheets_read_and_append(self):
        g, api = self.ws()
        api.files().list().execute.return_value = {"files": [{"id": "s1", "name": "Budget"}]}
        api.spreadsheets().values().get().execute.return_value = {"values": [["Rent", 500], ["Food", 200]]}
        out = g.sheets_read("budget", "A1:B2")
        self.assertIn("Rent, 500 | Food, 200", out)
        api.spreadsheets().values().append().execute.return_value = {}
        self.assertIn("Added a row (Gym, 30)", g.sheets_append("budget", "Gym, 30"))
        self.assertEqual(api.spreadsheets().values().append.call_args.kwargs["body"], {"values": [["Gym", "30"]]})

    def test_slides_and_forms(self):
        g, api = self.ws()
        self.assertIn("Created the presentation 'Pitch'", g.slides_create("Pitch"))
        self.assertIn("Created the form 'Survey'", g.forms_create("Survey"))
        api.files().list().execute.return_value = {"files": [{"id": "f1", "name": "Survey"}]}
        api.forms().responses().list().execute.return_value = {"responses": [{}, {}, {}]}
        self.assertIn("3 response(s)", g.forms_responses("survey"))

    def test_editing_existing_things_asks_first_but_creating_new_ones_does_not(self):
        g, _ = self.ws()
        risk = {t.name: t.risk for t in g.tools()}
        self.assertEqual(risk["docs_append"], "consequential")
        self.assertEqual(risk["sheets_append"], "consequential")
        for name in ("tasks_add", "docs_create", "slides_create", "forms_create", "contacts_search", "docs_read"):
            self.assertEqual(risk[name], "low", name)

    def test_connected_before_new_features_asks_to_reconnect(self):
        old = vault_with(**{TOKEN_KEY: {"token": "t", "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}})
        g = GoogleWorkspaceService(old)               # no injected build: the real scope check applies
        self.assertIn("connect google", g.tasks_list())
        self.assertIn("needs a permission", g.tasks_list())
        self.assertFalse(g._granted(TASKS))

    def test_not_connected_reports_a_failure(self):
        g = GoogleWorkspaceService(vault_with())
        out = Agent(FakeControl(), services=[g], policy=Policy()).handle("what's on my to do list")
        self.assertIn("0 of 1", out)

    def test_phrases(self):
        g, _ = self.ws()
        cases = {
            "what's on my to do list": ("tasks_list", {}),
            "my tasks": ("tasks_list", {}),
            "add buy milk to my to-do list": ("tasks_add", {"title": "buy milk"}),
            "remind me to call mom": ("tasks_add", {"title": "call mom"}),
            "find bob in my contacts": ("contacts_search", {"name": "bob"}),
            "what's alice's phone number": ("contacts_search", {"name": "alice"}),
            "create a google doc called trip plan": ("docs_create", {"title": "trip plan", "text": ""}),
            "read my doc trip plan": ("docs_read", {"name": "trip plan"}),
            "read sheet budget": ("sheets_read", {"name": "budget", "cells": "A1:F10"}),
            "create a presentation called pitch": ("slides_create", {"title": "pitch"}),
            "create a form called survey": ("forms_create", {"title": "survey"}),
        }
        for text, want in cases.items():
            self.assertEqual(g.resolve(text), want, text)
        for text in ("why is the sky blue", "open youtube", "check my email", "my name is bob", "read a book"):
            self.assertIsNone(g.resolve(text), text)

    def test_agent_asks_before_appending_to_a_sheet(self):
        g, api = self.ws()
        a = Agent(FakeControl(), services=[g], policy=Policy())
        a.services[0]  # registered
        out = a._run_new("log gym", [__import__("jarvis.core.agent", fromlist=["Step"]).Step(
            1, "sheets_append", {"name": "budget", "row": "Gym, 30"})], "local", None)
        self.assertIn("Say 'yes'", out)
        api.spreadsheets().values().append.assert_not_called()


class MapsTests(unittest.TestCase):
    def maps(self, response, **secrets):
        calls = []

        def http(method, url, headers, body=None):
            calls.append((method, url, headers, body))
            if isinstance(response, Exception):
                raise response
            return response
        return GoogleMapsService(vault_with(**secrets), http=http), calls

    def test_missing_key_explains_how_to_store_it(self):
        g, calls = self.maps({})
        self.assertIn("jarvis_secret.py set google_api_key", g.places_search("coffee"))
        self.assertEqual(calls, [])

    def test_places(self):
        g, calls = self.maps({"places": [{"displayName": {"text": "Blue Tokai"}, "formattedAddress": "MG Road", "rating": 4.6}]},
                             google_api_key="KEY")
        out = g.places_search("coffee near pune")
        self.assertIn("Blue Tokai (MG Road, rated 4.6)", out)
        self.assertEqual(calls[0][2]["X-Goog-Api-Key"], "KEY")
        self.assertEqual(calls[0][3]["textQuery"], "coffee near pune")

    def test_directions(self):
        g, calls = self.maps({"routes": [{"duration": "4500s", "distanceMeters": 120400}]}, google_api_key="K")
        out = g.maps_directions("pune", "mumbai", "driving")
        self.assertIn("120.4 km", out)
        self.assertIn("1 h 15 min", out)
        self.assertEqual(calls[0][3]["travelMode"], "DRIVE")
        self.assertEqual(g.maps_directions("a", "b", "teleport") and calls[-1][3]["travelMode"], "DRIVE")

    def test_no_route_and_api_errors_are_failures_not_crashes(self):
        g, _ = self.maps({"routes": []}, google_api_key="K")
        self.assertIn("Could not find a route", g.maps_directions("a", "b", "driving"))
        g, _ = self.maps(RuntimeError("HTTP 403 API not enabled"), google_api_key="K")
        self.assertIn("Could not search places: HTTP 403", g.places_search("x"))

    def test_web_search_needs_key_and_engine_id(self):
        g, _ = self.maps({}, google_api_key="K")
        self.assertIn("search engine id", g.web_search("cats"))
        g, calls = self.maps({"items": [{"title": "Cats", "link": "https://c.example"}]}, google_api_key="K", search_cx="CX")
        self.assertIn("Cats (https://c.example)", g.web_search("cats"))
        self.assertIn("cx=CX", calls[0][1])

    def test_closed_custom_search_is_reported_honestly(self):
        g, _ = self.maps(RuntimeError("HTTP 403 not available to new customers"), google_api_key="K", search_cx="CX")
        out = g.web_search("x")
        self.assertTrue(out.startswith("Could not"))
        self.assertIn("403", out)

    def test_phrases(self):
        g, _ = self.maps({})
        self.assertEqual(g.resolve("directions from pune to mumbai"),
                         ("maps_directions", {"origin": "pune", "destination": "mumbai", "mode": "driving"}))
        self.assertEqual(g.resolve("how far is it from delhi to agra")[1]["destination"], "agra")
        self.assertEqual(g.resolve("find coffee shops near koregaon park"),
                         ("places_search", {"query": "coffee shops near koregaon park"}))
        self.assertEqual(g.resolve("search the web for llama.cpp"), ("web_search", {"query": "llama.cpp"}))
        for text in ("find budget in my drive", "search my email for invoices", "open google", "why is the sky blue"):
            self.assertIsNone(g.resolve(text), text)

    def test_all_maps_tools_are_read_only(self):
        g, _ = self.maps({})
        self.assertTrue(all(t.risk == "low" and t.kind == "query" for t in g.tools()))


class WiringTests(unittest.TestCase):
    def test_all_services_load_and_tool_names_do_not_collide(self):
        services = load_services(None)
        self.assertEqual([s.name for s in services], ["google", "google-workspace", "google-maps"])
        names = [t.name for s in services for t in s.tools()]
        self.assertEqual(len(names), len(set(names)), "two services register the same tool")

    def test_one_phrase_never_matches_two_services(self):
        services = load_services(None)
        phrases = ["check my email", "what's on my calendar", "search drive for x", "my tasks", "find bob in my contacts",
                   "directions from a to b", "search the web for x", "connect google", "read sheet budget",
                   "remind me to call mom", "create a form called x"]
        for text in phrases:
            hits = [s.name for s in services if s.resolve(text)]
            self.assertEqual(len(hits), 1, f"{text!r} -> {hits}")

    def test_agent_registers_every_google_tool(self):
        a = Agent(FakeControl(), services=load_services(None), policy=Policy())
        for tool in ("gmail_send", "tasks_add", "docs_append", "sheets_append", "places_search", "web_search"):
            self.assertIn(tool, a.registry)


if __name__ == "__main__":
    unittest.main()
