"""gunicorn entrypoint: `gunicorn ref_mcp.wsgi:app` with REF_SERVER=clock|notes."""
import logging
import os

from . import clock, notes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_kind = os.environ.get("REF_SERVER", "")
_token = os.environ.get("MCP_AUTH_TOKEN") or None
if _token is None:
    raise RuntimeError("MCP_AUTH_TOKEN is required: refusing to start an unauthenticated MCP server")

if _kind == "clock":
    app = clock.build_app(_token).flask_app()
elif _kind == "notes":
    app = notes.build_app(notes.NoteStore(os.environ.get("NOTES_PATH", "/data/notes.json")), _token).flask_app()
else:
    raise RuntimeError("REF_SERVER must be 'clock' or 'notes'")
