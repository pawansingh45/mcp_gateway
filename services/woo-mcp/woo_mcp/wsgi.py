"""gunicorn entrypoint: `gunicorn woo_mcp.wsgi:app`"""
import logging

from .tools import from_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = from_env().flask_app()
