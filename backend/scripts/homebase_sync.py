"""Run daily by Windows Task Scheduler (see README for the exact setup
steps). Reuses the session saved by homebase_login.py — does not log in
itself. Writes straight to the same database the app uses, so it works
whether or not the web server happens to be running at the time.

Manual run:
    ..\\.venv\\Scripts\\python scripts\\homebase_sync.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.services.homebase_browser import sync_once  # noqa: E402

if __name__ == "__main__":
    db = SessionLocal()
    try:
        result = sync_once(db)
    finally:
        db.close()

    if result["hours_rows"] or result["swap_rows"]:
        print(f"Synced {result['hours_rows']} hours row(s), {result['swap_rows']} shift swap(s).")
    if result["error"]:
        print(f"Issues: {result['error']}")
    if not result["ok"] and not (result["hours_rows"] or result["swap_rows"]):
        sys.exit(1)  # nothing at all synced — a real failure, not just one report needing calibration
