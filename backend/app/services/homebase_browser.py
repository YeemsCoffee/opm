"""Drives a real (Playwright-controlled) browser against Homebase, reusing
a saved login session instead of storing a password anywhere.

Two entry points:
  login_interactively()  — one-time, opens a VISIBLE window; a human logs in
  sync_once(db, ...)      — reuses the saved session headlessly; called by
                            the scheduled script (or the "Sync now" button)

The saved session lives in PROFILE_DIR, a normal Chromium profile folder.
Nothing about the account password is ever written to disk by this code —
only whatever cookies Chromium itself stores after a real login, exactly
like closing and reopening a browser tab.
"""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, HomebaseSyncStatus, HoursSnapshot, ShiftSwap
from .homebase_grid_parser import PositionedText, find_open_shift_pickups
from .homebase_table_parser import parse_hours_table

PROFILE_DIR = Path(os.environ.get("HOMEBASE_PROFILE_DIR", Path(__file__).parent.parent.parent / ".homebase_profile"))
CONFIG_PATH = Path(os.environ.get("HOMEBASE_SCRAPE_CONFIG", Path(__file__).parent.parent.parent / "homebase_scrape_config.json"))
LOGIN_WAIT_TIMEOUT_MS = 10 * 60 * 1000  # give a human up to 10 minutes to log in

# Runs in the browser: collects every leaf (childless) element with visible,
# non-empty text and its rendered position. Used to locate day-column
# headers, employee-row names, and flagged shift blocks by position alone —
# no dependency on Homebase's DOM structure or class names.
_EXTRACT_LEAF_TEXT_JS = """
() => {
  const out = [];
  const all = document.querySelectorAll('body *');
  for (const el of all) {
    if (el.children.length > 0) continue;
    const text = (el.textContent || '').trim();
    if (!text) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    out.push({ text, x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 });
  }
  return out;
}
"""


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def login_interactively() -> None:
    """Opens a real, visible browser window on THIS machine for a human to
    log into Homebase. Run this once (and again whenever the saved session
    expires) directly on the machine that will run the daily sync — not
    inside any remote/cloud session."""
    cfg = load_config()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(cfg["login_url"])
        print("A browser window has opened. Log into Homebase normally.")
        print("This window will close itself once you're logged in — leave it open until then.")
        try:
            page.wait_for_url(lambda url: "login" not in url.lower(), timeout=LOGIN_WAIT_TIMEOUT_MS)
            print("Login detected — session saved. You can close this and run the sync script.")
        except Exception:
            print("Timed out waiting for login. Run this script again when you're ready.")
        context.close()


def _session_looks_logged_out(url: str) -> bool:
    return "login" in url.lower() or "sign_in" in url.lower()


def sync_once(db: Session, period_start: date | None = None, period_end: date | None = None) -> dict:
    """Scrapes the hours report and trade board using the saved session.
    Never attempts to log in itself — if the session is gone, it reports
    that clearly and leaves existing data untouched."""
    cfg = load_config()
    status = db.scalar(select(HomebaseSyncStatus)) or HomebaseSyncStatus()
    if status.id is None:
        db.add(status)
    status.last_attempt_at = datetime.utcnow()

    if not PROFILE_DIR.exists():
        status.session_valid = False
        status.last_error = "No saved Homebase session yet — run the one-time login script first."
        db.commit()
        return {"ok": False, "error": status.last_error}

    if period_start is None or period_end is None:
        today = date.today()
        period_start = today - timedelta(days=today.weekday())  # Monday of this week
        period_end = period_start + timedelta(days=6)

    hours_rows: list[dict] = []
    swap_rows: list[dict] = []
    hours_ok = swaps_ok = False
    session_expired = False
    errors: list[str] = []

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
            page = context.pages[0] if context.pages else context.new_page()

            # Each report is scraped independently: one failing (e.g. the
            # trade board's URL/columns still need calibration) must never
            # discard data the other one successfully collected.
            hb = cfg["hours_report"]
            try:
                page.goto(cfg["base_url"] + hb["path"])
                page.wait_for_load_state("networkidle")
                if _session_looks_logged_out(page.url):
                    session_expired = True
                    raise RuntimeError("Homebase session expired — please re-run the login script.")
                hours_rows = parse_hours_table(
                    page.content(), hb["name_header_keywords"], hb["worked_header_keywords"]
                )
                hours_ok = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"hours: {exc}")

            if not session_expired:
                tb = cfg["trade_board"]
                try:
                    week_anchor = date.today()
                    page.goto(cfg["base_url"] + tb["path_template"].format(date=week_anchor.isoformat()))
                    page.wait_for_load_state("networkidle")
                    if _session_looks_logged_out(page.url):
                        session_expired = True
                        raise RuntimeError("Homebase session expired — please re-run the login script.")
                    raw = page.evaluate(_EXTRACT_LEAF_TEXT_JS)
                    positioned = [PositionedText(r["text"], r["x"], r["y"]) for r in raw]
                    employee_names = [n for (n,) in db.execute(select(Employee.name))]
                    swap_rows = find_open_shift_pickups(
                        positioned, tb["marker_keywords"], employee_names, week_anchor
                    )
                    swaps_ok = True
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"swaps: {exc}")

            context.close()
    except Exception as exc:  # noqa: BLE001 — e.g. the browser itself failed to launch
        errors.append(f"browser: {exc}")

    if session_expired:
        status.session_valid = False
        status.last_error = "; ".join(errors)
        db.commit()
        return {"ok": False, "hours_rows": 0, "swap_rows": 0, "error": status.last_error}

    for row in hours_rows:
        existing = db.scalar(
            select(HoursSnapshot).where(
                HoursSnapshot.employee_name == row["name"],
                HoursSnapshot.period_start == period_start,
                HoursSnapshot.period_end == period_end,
            )
        )
        if existing:
            existing.hours = row["hours"]
            existing.synced_at = datetime.utcnow()
        else:
            db.add(
                HoursSnapshot(
                    employee_name=row["name"],
                    period_start=period_start,
                    period_end=period_end,
                    hours=row["hours"],
                )
            )

    # Only the picker's identity + date is collected (confirmed sufficient) —
    # released_by/role/times stay blank/null for rows from this scrape path.
    for row in swap_rows:
        existing = db.scalar(
            select(ShiftSwap).where(
                ShiftSwap.shift_date == row["shift_date"],
                ShiftSwap.covered_by == row["employee_name"],
            )
        )
        if existing:
            existing.synced_at = datetime.utcnow()
        else:
            db.add(
                ShiftSwap(
                    shift_date=row["shift_date"],
                    covered_by=row["employee_name"],
                    status="Open Shift approved",
                )
            )

    status.session_valid = True
    status.last_error = "; ".join(errors)
    if hours_ok or swaps_ok:
        status.last_success_at = datetime.utcnow()
    status.hours_rows_last_sync = len(hours_rows)
    status.swaps_rows_last_sync = len(swap_rows)
    db.commit()
    return {
        "ok": not errors,
        "hours_rows": len(hours_rows),
        "swap_rows": len(swap_rows),
        "error": "; ".join(errors),
    }
