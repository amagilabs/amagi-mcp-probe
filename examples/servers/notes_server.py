"""A well-behaved example MCP server: a small in-memory notes store.

    amagi-mcp-probe -- python examples/servers/notes_server.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from minimcp import ToolError, serve, text  # noqa: E402

NOTES = {
    "note-001": {"title": "Release checklist", "tags": ["release"], "body": "Tag, build, publish."},
    "note-002": {"title": "Standup", "tags": ["team"], "body": "Demo the probe."},
}


def add_note(args):
    note_id = f"note-{len(NOTES) + 1:03d}"
    NOTES[note_id] = {"title": args["title"], "tags": args.get("tags", []), "body": args.get("body", "")}
    return text(f"created {note_id}", {"id": note_id})


def get_note(args):
    note = NOTES.get(args["id"])
    if note is None:
        raise ToolError(f"no note with id {args['id']}")
    return text(note["body"], {"id": args["id"], "title": note["title"], "tags": note["tags"]})


def search_notes(args):
    query = args["query"].lower()
    hits = [i for i, n in NOTES.items() if query in n["title"].lower() or query in n["body"].lower()]
    hits = hits[: args.get("limit", 10)]
    return text(", ".join(hits) or "no matches", {"ids": hits})


NOTE_OUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "title", "tags"],
}

TOOLS = [
    {
        "name": "add_note",
        "description": "Create a note and return its id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string", "pattern": "^[a-z]+$"}, "maxItems": 5},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        "handler": add_note,
    },
    {
        "name": "get_note",
        "description": "Fetch one note by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "pattern": "^note-[0-9]{3}$"}},
            "required": ["id"],
        },
        "outputSchema": NOTE_OUT,
        "annotations": {"readOnlyHint": True},
        "handler": get_note,
    },
    {
        "name": "search_notes",
        "description": "Search note titles and bodies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["ids"],
        },
        "annotations": {"readOnlyHint": True},
        "handler": search_notes,
    },
]

if __name__ == "__main__":
    serve("notes", "1.0.0", TOOLS)
