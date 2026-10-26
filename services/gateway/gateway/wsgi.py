"""gunicorn entrypoint: `gunicorn gateway.wsgi:app` (single process, many threads - see Dockerfile)."""
import logging

from .app import create_app
from .config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app(Settings.from_env())
