"""Run this ONCE (and again whenever the daily sync reports the session has
expired) directly on the machine that will run the scheduled sync — e.g.
your own Windows PC, not a remote session.

    ..\\.venv\\Scripts\\python scripts\\homebase_login.py

A real, visible Chrome window opens. Log into Homebase exactly like you
normally would. Once logged in, close the window (or wait — it closes
itself a moment after it detects you're in). That's it: no password is
ever stored by this script, only the ordinary browser session Chromium
itself keeps.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.homebase_browser import login_interactively  # noqa: E402

if __name__ == "__main__":
    login_interactively()
