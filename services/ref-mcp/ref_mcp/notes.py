"""'notes' MCP server: a tiny stateful scratchpad (save / list / search / delete).

State is a JSON file so notes survive restarts when a volume is mounted at
NOTES_PATH's directory. Writes are atomic (temp file + rename) and serialised
with a lock; this server is meant for a single replica.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from mcpkit import McpApp, ToolError

MAX_NOTES = 500
MAX_BODY = 10_000


class NoteStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            return []  # corrupted file: start fresh rather than crash-loop

    def _save(self, notes: list[dict]) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(notes, fh)
        os.replace(tmp, self.path)

    def add(self, title: str, body: str) -> dict:
        with self.lock:
            notes = self._load()
            if len(notes) >= MAX_NOTES:
                raise ToolError(f"Note limit reached ({MAX_NOTES}). Delete some notes first.")
            note = {"id": uuid.uuid4().hex[:8], "title": title,
                    "body": body, "created": datetime.now(timezone.utc).isoformat()}
            notes.append(note)
            self._save(notes)
            return note

    def all(self) -> list[dict]:
        with self.lock:
            return self._load()

    def delete(self, note_id: str) -> bool:
        with self.lock:
            notes = self._load()
            kept = [n for n in notes if n["id"] != note_id]
            if len(kept) == len(notes):
                return False
            self._save(kept)
            return True


def build_app(store: NoteStore, auth_token: str | None = None) -> McpApp:
    app = McpApp("notes-mcp", auth_token=auth_token, instructions="A shared team scratchpad for short notes.")

    @app.tool("save_note", "Save a note (title + body).",
              {"properties": {"title": {"type": "string"}, "body": {"type": "string"}},
               "required": ["title", "body"]})
    def save_note(a):
        title, body = a["title"].strip(), a["body"]
        if not title:
            raise ToolError("title must not be empty")
        if len(body) > MAX_BODY:
            raise ToolError(f"body too long (max {MAX_BODY} characters)")
        return store.add(title[:200], body)

    @app.tool("list_notes", "List all saved notes, newest first.", {"properties": {}}, read_only=True)
    def list_notes(a):
        notes = sorted(store.all(), key=lambda n: n["created"], reverse=True)
        return {"count": len(notes), "notes": notes}

    @app.tool("search_notes", "Find notes whose title or body contains the text (case-insensitive).",
              {"properties": {"query": {"type": "string"}}, "required": ["query"]}, read_only=True)
    def search_notes(a):
        q = a["query"].lower()
        hits = [n for n in store.all() if q in n["title"].lower() or q in n["body"].lower()]
        return {"count": len(hits), "notes": hits}

    @app.tool("delete_note", "Delete a note by id.",
              {"properties": {"id": {"type": "string"}}, "required": ["id"]})
    def delete_note(a):
        if not store.delete(a["id"]):
            raise ToolError(f"No note with id {a['id']}")
        return {"deleted": a["id"]}

    return app
