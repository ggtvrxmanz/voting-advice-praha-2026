# Makes `import pipeline` work in the tests the same way it works in production.
# Render starts the app with `gunicorn --chdir app server:app`, so at run time `app/` is
# the working directory and `pipeline` is a top-level module. Putting app/ on sys.path
# here keeps the import identical, instead of testing a package layout we never deploy.
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
