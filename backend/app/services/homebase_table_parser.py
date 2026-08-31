"""Pure HTML-table parsing for the Homebase scraper.

Kept separate from the Playwright driver (homebase_browser.py) so the
column-matching logic can be unit tested against saved sample HTML without
a real browser or a live Homebase session. When the real pages are
available, adjust HEADER_KEYWORDS in homebase_scrape_config.json rather
than this code — that's the calibration step flagged to the user.
"""

import re

from bs4 import BeautifulSoup

# Matches Homebase's "Worked" cell format, e.g. "Total: 8 hrs 20 min" or
# "Total: 5 hrs" (no minutes). A no-show row has no "Total:" line at all —
# that's how a no-show is distinguished from a real zero-hour shift.
_TOTAL_RE = re.compile(r"total:\s*(\d+)\s*hrs?(?:\s*(\d+)\s*min)?", re.IGNORECASE)


def extract_hours_total(cell_text: str) -> float | None:
    m = _TOTAL_RE.search(cell_text)
    if not m:
        return None
    hours = int(m.group(1))
    minutes = int(m.group(2)) if m.group(2) else 0
    return round(hours + minutes / 60, 2)


# Fields that identify a person: cells for these often carry extra lines
# below the name (role, hours, pay) — e.g. "Allen Tran\nB2" — so only the
# first line is kept. Every other field is collapsed to one normalized,
# regex-searchable string (e.g. the Worked cell's several lines).
_NAME_LIKE_FIELDS = {"name", "covered_by", "released_by"}


def _norm(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _cell_text(cell, field: str) -> str:
    raw = cell.get_text(separator="\n", strip=True)
    if field in _NAME_LIKE_FIELDS:
        first_line = raw.split("\n", 1)[0] if raw else ""
        return _norm(first_line)
    return _norm(raw.replace("\n", " "))


def _match_column(headers: list[str], keywords: list[str]) -> int | None:
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i
    return None


def find_best_table(html: str, column_keywords: dict[str, list[str]]) -> tuple[list[dict], list[str]]:
    """Scan every <table> on the page; return the rows of whichever table
    matches the most requested columns, as a list of {field: text} dicts,
    plus the list of fields that could not be matched to any column (so the
    caller can report a clear calibration error instead of silently
    returning empty/partial data)."""
    soup = BeautifulSoup(html, "html.parser")
    best_rows: list[dict] = []
    best_score = -1
    best_missing: list[str] = list(column_keywords)

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        headers = [_norm(c.get_text(separator=" ")) for c in header_cells]
        if not headers:
            continue

        mapping: dict[str, int] = {}
        for field, keywords in column_keywords.items():
            idx = _match_column(headers, keywords)
            if idx is not None:
                mapping[field] = idx
        if len(mapping) <= max(best_score, 0):
            continue

        parsed = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            record = {
                field: _cell_text(cells[idx], field) if idx < len(cells) else ""
                for field, idx in mapping.items()
            }
            if any(record.values()):
                parsed.append(record)

        best_score = len(mapping)
        best_rows = parsed
        best_missing = [f for f in column_keywords if f not in mapping]

    return best_rows, best_missing


def parse_hours_table(html: str, name_keywords: list[str], worked_keywords: list[str]) -> list[dict]:
    """Homebase's Timesheets table has a "Team member" column and a
    "Worked" column whose text embeds the total, e.g. "9:28 am - 5:48 pm
    Total: 8 hrs 20 min". A no-show row has no "Total:" line and is
    skipped — it's tracked separately, not as zero hours worked."""
    rows, missing = find_best_table(html, {"name": name_keywords, "worked": worked_keywords})
    if missing:
        raise ValueError(
            f"Hours report page: could not find column(s) {missing} — "
            "the scrape config's header keywords need calibration against the real page."
        )
    out = []
    for r in rows:
        if not r["name"]:
            continue
        hours = extract_hours_total(r["worked"])
        if hours is not None:
            out.append({"name": r["name"].title(), "hours": hours})
    return out


def parse_swaps_table(
    html: str,
    date_keywords: list[str],
    released_by_keywords: list[str],
    covered_by_keywords: list[str],
    role_keywords: list[str],
    status_keywords: list[str],
) -> list[dict]:
    rows, missing = find_best_table(
        html,
        {
            "date": date_keywords,
            "covered_by": covered_by_keywords,
            "released_by": released_by_keywords,
            "role": role_keywords,
            "status": status_keywords,
        },
    )
    # date + covered_by are the two fields this feature actually needs;
    # released_by/role/status are nice-to-have and may not exist on every
    # Homebase layout, so don't hard-fail on those alone
    required_missing = [f for f in ("date", "covered_by") if f in missing]
    if required_missing:
        raise ValueError(
            f"Trade board page: could not find column(s) {required_missing} — "
            "the scrape config's header keywords need calibration against the real page."
        )
    return [r for r in rows if r.get("covered_by")]
