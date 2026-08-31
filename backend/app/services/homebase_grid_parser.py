"""Pure geometry-based parsing for Homebase's weekly Schedule Builder grid.

This page (confirmed against a real screenshot) is a visual calendar grid,
not a semantic table — employee rows down the left, day columns across the
top, shift blocks positioned within. A shift released for coverage and
picked up shows as a flagged block (e.g. "Open Shift approved") sitting in
the covering employee's row.

Rather than depend on Homebase's exact DOM structure (div classes, nesting
— none of which is known), this matches purely on rendered position: each
flagged block is assigned to whichever day-header sits closest on the x
axis, and whichever employee name sits closest on the y axis. That's
robust to markup changes in a way a class-name or table selector isn't.

The (text, x, y) triples themselves come from a live page via Playwright's
page.evaluate (see homebase_browser.py) — kept out of this module so the
matching logic can be unit tested with synthetic coordinates.
"""

from dataclasses import dataclass
from datetime import date, timedelta

_DAY_HEADER_RE = None  # compiled lazily to avoid import cost if unused


def _day_header_pattern():
    import re

    global _DAY_HEADER_RE
    if _DAY_HEADER_RE is None:
        _DAY_HEADER_RE = re.compile(
            r"^(sun|mon|tue|wed|thu|fri|sat)\w*,?\s*\d{1,2}$", re.IGNORECASE
        )
    return _DAY_HEADER_RE


@dataclass
class PositionedText:
    text: str
    x: float
    y: float


def week_start_for(anchor: date) -> date:
    """Homebase's schedule weeks run Sunday–Saturday; returns the Sunday of
    the week containing `anchor`."""
    return anchor - timedelta(days=(anchor.weekday() + 1) % 7)


def find_day_headers(elements: list[PositionedText]) -> list[PositionedText]:
    pattern = _day_header_pattern()
    matches = [e for e in elements if pattern.match(e.text.strip())]
    return sorted(matches, key=lambda e: e.x)


def find_employee_rows(
    elements: list[PositionedText], known_names: list[str]
) -> list[PositionedText]:
    by_lower = {n.lower(): n for n in known_names}
    out = []
    for e in elements:
        name = by_lower.get(e.text.strip().lower())
        if name:
            out.append(PositionedText(text=name, x=e.x, y=e.y))
    return out


def find_open_shift_pickups(
    elements: list[PositionedText],
    marker_keywords: list[str],
    known_employee_names: list[str],
    week_anchor: date,
) -> list[dict]:
    """Returns [{employee_name, shift_date}] — one per flagged shift block,
    deduplicated. Returns [] (not an error) when there's nothing to find;
    a week with zero pickups is a legitimate result, not a failure."""
    headers = find_day_headers(elements)
    if len(headers) < 2:
        return []  # can't map columns to dates with fewer than 2 reference points
    rows = find_employee_rows(elements, known_employee_names)
    if not rows:
        return []

    week_start = week_start_for(week_anchor)
    markers = [
        e for e in elements if any(kw.lower() in e.text.lower() for kw in marker_keywords)
    ]

    seen: set[tuple[str, date]] = set()
    out = []
    for m in markers:
        nearest_header = min(headers, key=lambda h: abs(h.x - m.x))
        day_index = headers.index(nearest_header)
        shift_date = week_start + timedelta(days=day_index)

        nearest_row = min(rows, key=lambda r: abs(r.y - m.y))
        key = (nearest_row.text, shift_date)
        if key in seen:
            continue
        seen.add(key)
        out.append({"employee_name": nearest_row.text, "shift_date": shift_date})

    return out
