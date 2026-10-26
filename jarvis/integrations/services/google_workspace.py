"""Google Workspace through the official APIs: Contacts, Tasks, Docs, Sheets, Slides, Forms.

Shares the login of GoogleService (one "connect google" grants everything). Reading, and creating
new items in your own account, run at once. Changing something that already exists (appending to a
document or a spreadsheet) is consequential, so Jarvis says what it will do and waits for "yes".
"""
from __future__ import annotations

import re

from jarvis.core.agent.tools import Tool
from jarvis.integrations.services.google import (CONTACTS, DOCS, DRIVE_READ, FORMS, FORMS_RESPONSES, SHEETS,
                                                 SLIDES, TASKS, GoogleService)

MAX_ITEMS = 8
MIME = {
    "doc": "application/vnd.google-apps.document",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "form": "application/vnd.google-apps.form",
    "slides": "application/vnd.google-apps.presentation",
}
MAX_TEXT = 1500            # characters of a document read aloud


class GoogleWorkspaceService(GoogleService):
    name = "google-workspace"
    description = "Google Contacts, Tasks, Docs, Sheets, Slides and Forms"

    # ---- helpers ----------------------------------------------------------------------
    def _find(self, kind: str, name: str):
        """(file id, title) of the best Drive match for a name, or (None, message)."""
        svc = self._api("drive", "v3")
        if svc is None:
            return None, "Could not use Google: it is not connected yet. Say 'connect google'."
        if not self._build and not self._granted(DRIVE_READ):
            return None, "Could not search Drive: reconnect Google ('connect google')."
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        files = svc.files().list(q=f"name contains '{safe}' and mimeType='{MIME[kind]}' and trashed=false",
                                 pageSize=1, orderBy="modifiedTime desc",
                                 fields="files(id,name)").execute().get("files", [])
        if not files:
            return None, f"Could not find a Google {kind} called '{name}'."
        return files[0]["id"], files[0]["name"]

    # ---- Contacts ---------------------------------------------------------------------
    def contacts_search(self, name: str) -> str:
        def run(svc):
            # searchContacts needs a warm-up request with an empty query before it returns results
            svc.people().searchContacts(query="", readMask="names").execute()
            hits = svc.people().searchContacts(query=name, pageSize=MAX_ITEMS,
                                               readMask="names,emailAddresses,phoneNumbers").execute().get("results", [])
            if not hits:
                return f"No contacts found matching '{name}'."
            lines = []
            for h in hits:
                p = h.get("person", {})
                n = (p.get("names") or [{}])[0].get("displayName", "(no name)")
                bits = [e["value"] for e in p.get("emailAddresses", [])[:1]] + [t["value"] for t in p.get("phoneNumbers", [])[:1]]
                lines.append(n + (f" ({', '.join(bits)})" if bits else ""))
            return f"{len(lines)} contact(s): " + "; ".join(lines) + "."
        return self._call("people", "v1", run, CONTACTS)

    # ---- Tasks ------------------------------------------------------------------------
    def tasks_list(self) -> str:
        def run(svc):
            items = svc.tasks().list(tasklist="@default", showCompleted=False, maxResults=MAX_ITEMS).execute().get("items", [])
            if not items:
                return "Your to-do list is empty."
            return f"{len(items)} task(s): " + "; ".join(t.get("title", "(untitled)") for t in items) + "."
        return self._call("tasks", "v1", run, TASKS)

    def tasks_add(self, title: str) -> str:
        if not title.strip():
            return "Could not add a task: it had no title."

        def run(svc):
            t = svc.tasks().insert(tasklist="@default", body={"title": title.strip()}).execute()
            return f"Added '{t.get('title', title.strip())}' to your to-do list."
        return self._call("tasks", "v1", run, TASKS)

    # ---- Docs -------------------------------------------------------------------------
    def docs_create(self, title: str, text: str) -> str:
        def run(svc):
            doc = svc.documents().create(body={"title": title}).execute()
            if text.strip():
                svc.documents().batchUpdate(documentId=doc["documentId"], body={"requests": [
                    {"insertText": {"location": {"index": 1}, "text": text}}]}).execute()
            return f"Created the Google Doc '{title}'."
        return self._call("docs", "v1", run, DOCS)

    def docs_read(self, name: str) -> str:
        def run(svc):
            doc_id, title = self._find("doc", name)
            if doc_id is None:
                return title
            doc = svc.documents().get(documentId=doc_id).execute()
            parts = [el.get("textRun", {}).get("content", "")
                     for blk in doc.get("body", {}).get("content", [])
                     for el in blk.get("paragraph", {}).get("elements", [])]
            text = " ".join("".join(parts).split())
            return f"{title}: " + (text[:MAX_TEXT] + ("..." if len(text) > MAX_TEXT else "") if text else "(empty)")
        return self._call("docs", "v1", run, DOCS)

    def docs_append(self, name: str, text: str) -> str:
        def run(svc):
            doc_id, title = self._find("doc", name)
            if doc_id is None:
                return title
            doc = svc.documents().get(documentId=doc_id).execute()
            end = max(1, doc.get("body", {}).get("content", [{}])[-1].get("endIndex", 2) - 1)
            svc.documents().batchUpdate(documentId=doc_id, body={"requests": [
                {"insertText": {"location": {"index": end}, "text": "\n" + text}}]}).execute()
            return f"Added your text to '{title}'."
        return self._call("docs", "v1", run, DOCS)

    # ---- Sheets -----------------------------------------------------------------------
    def sheets_read(self, name: str, cells: str) -> str:
        def run(svc):
            sid, title = self._find("sheet", name)
            if sid is None:
                return title
            rows = svc.spreadsheets().values().get(spreadsheetId=sid, range=cells).execute().get("values", [])
            if not rows:
                return f"{title} has nothing in {cells}."
            return f"{title} {cells}: " + " | ".join(", ".join(str(c) for c in r) for r in rows[:MAX_ITEMS * 2]) + "."
        return self._call("sheets", "v4", run, SHEETS)

    def sheets_append(self, name: str, row: str) -> str:
        values = [c.strip() for c in row.split(",")]

        def run(svc):
            sid, title = self._find("sheet", name)
            if sid is None:
                return title
            svc.spreadsheets().values().append(spreadsheetId=sid, range="A1", valueInputOption="USER_ENTERED",
                                               insertDataOption="INSERT_ROWS", body={"values": [values]}).execute()
            return f"Added a row ({', '.join(values)}) to '{title}'."
        return self._call("sheets", "v4", run, SHEETS)

    # ---- Slides and Forms -------------------------------------------------------------
    def slides_create(self, title: str) -> str:
        def run(svc):
            svc.presentations().create(body={"title": title}).execute()
            return f"Created the presentation '{title}'."
        return self._call("slides", "v1", run, SLIDES)

    def forms_create(self, title: str) -> str:
        def run(svc):
            svc.forms().create(body={"info": {"title": title, "documentTitle": title}}).execute()
            return f"Created the form '{title}'."
        return self._call("forms", "v1", run, FORMS)

    def forms_responses(self, name: str) -> str:
        def run(svc):
            fid, title = self._find("form", name)
            if fid is None:
                return title
            resp = svc.forms().responses().list(formId=fid).execute().get("responses", [])
            return f"'{title}' has {len(resp)} response(s)."
        return self._call("forms", "v1", run, FORMS_RESPONSES)

    # ---- Service interface ------------------------------------------------------------
    def tools(self) -> list[Tool]:
        return [
            Tool("contacts_search", "Look up a contact's email or phone number.", self.contacts_search,
                 {"name": "person to find"}, kind="query"),
            Tool("tasks_list", "List the open items on the Google Tasks to-do list.", self.tasks_list, kind="query"),
            Tool("tasks_add", "Add an item to the Google Tasks to-do list.", self.tasks_add, {"title": "task text"}),
            Tool("docs_create", "Create a new Google Doc with some text.", self.docs_create,
                 {"title": "document title", "text": "body text (may be empty)"}),
            Tool("docs_read", "Read the text of a Google Doc found by name.", self.docs_read,
                 {"name": "part of the document name"}, kind="query"),
            Tool("docs_append", "Add text to the end of an existing Google Doc.", self.docs_append,
                 {"name": "part of the document name", "text": "text to add"}, risk="consequential", retryable=False,
                 describe=lambda a: f"add text to your document '{a.get('name')}'"),
            Tool("sheets_read", "Read cells from a Google Sheet found by name.", self.sheets_read,
                 {"name": "part of the sheet name", "cells": "range such as A1:D10"}, kind="query"),
            Tool("sheets_append", "Append a row to a Google Sheet.", self.sheets_append,
                 {"name": "part of the sheet name", "row": "comma-separated values"},
                 risk="consequential", retryable=False,
                 describe=lambda a: f"add the row '{a.get('row')}' to your spreadsheet '{a.get('name')}'"),
            Tool("slides_create", "Create a new Google Slides presentation.", self.slides_create,
                 {"title": "presentation title"}),
            Tool("forms_create", "Create a new Google Form.", self.forms_create, {"title": "form title"}),
            Tool("forms_responses", "Count the responses a Google Form has received.", self.forms_responses,
                 {"name": "part of the form name"}, kind="query"),
        ]

    _TASKS = re.compile(r"^(?:what(?:'s| is)?\s+on\s+)?(?:show\s+(?:me\s+)?|read\s+|check\s+)?my\s+(?:to-?\s?do(?:s)?(?:\s+list)?|tasks?(?:\s+list)?)$"
                        r"|^what\s+do\s+i\s+(?:need|have)\s+to\s+do$")
    _TASK_ADD = re.compile(r"^(?:add|put)\s+(.+?)\s+(?:to|on)\s+my\s+(?:to-?\s?do(?:\s+list)?|tasks?(?:\s+list)?)$"
                           r"|^remind\s+me\s+to\s+(.+)$")
    _CONTACT = re.compile(r"^(?:find|look\s+up|search\s+for)\s+(.+?)\s+in\s+my\s+contacts$"
                          r"|^what(?:'s| is)\s+(.+?)(?:'s|s)\s+(?:phone\s+)?(?:number|email|e-?mail(?:\s+address)?)$"
                          r"|^(?:phone\s+number|email(?:\s+address)?)\s+(?:of|for)\s+(.+)$")
    _DOC_NEW = re.compile(r"^(?:create|make|start|new)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:google\s+)?doc(?:ument)?"
                          r"\s+(?:called|named|titled)\s+(.+)$")
    _DOC_READ = re.compile(r"^(?:read|open|show)\s+(?:me\s+)?(?:my\s+)?(?:google\s+)?doc(?:ument)?\s+(?:called\s+|named\s+)?(.+)$")
    _SHEET_READ = re.compile(r"^(?:read|show|open)\s+(?:me\s+)?(?:my\s+)?(?:google\s+)?(?:sheet|spreadsheet)\s+(?:called\s+|named\s+)?(.+)$")
    _SLIDES_NEW = re.compile(r"^(?:create|make|start|new)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:google\s+)?(?:presentation|slides?|slide\s+deck)"
                             r"\s+(?:called|named|titled)\s+(.+)$")
    _FORM_NEW = re.compile(r"^(?:create|make|start|new)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:google\s+)?form\s+(?:called|named|titled)\s+(.+)$")
    _FORM_RESP = re.compile(r"^(?:how\s+many\s+)?(?:responses?|submissions?)\s+(?:has|for|does|on)\s+(?:my\s+)?(?:form\s+)?(.+?)(?:\s+(?:got|have|has))?$")

    def resolve(self, text: str):
        q = text.strip().lower().rstrip(" .!?,;")
        if self._TASKS.match(q):
            return "tasks_list", {}
        m = self._TASK_ADD.match(q)
        if m:
            return "tasks_add", {"title": (m.group(1) or m.group(2)).strip()}
        m = self._CONTACT.match(q)
        if m:
            return "contacts_search", {"name": next(g for g in m.groups() if g).strip()}
        m = self._DOC_NEW.match(q)
        if m:
            return "docs_create", {"title": m.group(1).strip(), "text": ""}
        m = self._DOC_READ.match(q)
        if m:
            return "docs_read", {"name": m.group(1).strip()}
        m = self._SHEET_READ.match(q)
        if m:
            return "sheets_read", {"name": m.group(1).strip(), "cells": "A1:F10"}
        m = self._SLIDES_NEW.match(q)
        if m:
            return "slides_create", {"title": m.group(1).strip()}
        m = self._FORM_NEW.match(q)
        if m:
            return "forms_create", {"title": m.group(1).strip()}
        m = self._FORM_RESP.match(q)
        if m and "form" in q:
            return "forms_responses", {"name": m.group(1).strip()}
        return None
