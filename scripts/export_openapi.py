"""Write the API's OpenAPI document to a file (the web app generates its TypeScript types from it):

    uv run python scripts/export_openapi.py web/openapi.json
"""
import json
import sys
from pathlib import Path

from billparse.api import app

Path(sys.argv[1]).write_text(json.dumps(app.openapi(), indent=1) + "\n")
