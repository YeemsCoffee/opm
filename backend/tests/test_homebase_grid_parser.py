from datetime import date

from app.services.homebase_grid_parser import (
    PositionedText,
    find_open_shift_pickups,
    week_start_for,
)

# Coordinates loosely modeled on a real Homebase schedule screenshot: day
# headers along the top (Sun..Sat, left to right), employee names in a
# left column, shift blocks positioned within each employee's row/day.
DAY_HEADERS = [
    PositionedText("Sun, 30", x=100, y=10),
    PositionedText("Mon, 31", x=200, y=10),
    PositionedText("Tue, 1", x=300, y=10),
    PositionedText("Wed, 2", x=400, y=10),
    PositionedText("Thu, 3", x=500, y=10),
    PositionedText("Fri, 4", x=600, y=10),
    PositionedText("Sat, 5", x=700, y=10),
]
EMPLOYEE_ROWS = [
    PositionedText("Luis Escobar", x=20, y=100),
    PositionedText("Lynna Carmichael", x=20, y=180),
    PositionedText("Mari Shozi", x=20, y=260),
    PositionedText("Paula Cruz-Bonde", x=20, y=420),
]
KNOWN_NAMES = [e.text for e in EMPLOYEE_ROWS]
WEEK_ANCHOR = date(2026, 8, 31)  # a Monday; the week's Sunday is Aug 30


def test_week_start_for_finds_sunday():
    assert week_start_for(date(2026, 8, 31)) == date(2026, 8, 30)  # Monday
    assert week_start_for(date(2026, 8, 30)) == date(2026, 8, 30)  # Sunday itself
    assert week_start_for(date(2026, 9, 5)) == date(2026, 8, 30)  # Saturday


def test_matches_marker_to_nearest_row_and_column():
    elements = [
        *DAY_HEADERS,
        *EMPLOYEE_ROWS,
        PositionedText("7:30am-1pm K1", x=195, y=105),  # ordinary shift, Luis, Monday
        PositionedText(
            "6:30am-11:30am K1 ⚠ Open Shift approved", x=198, y=418
        ),  # Paula, Monday
        PositionedText("Unavailable All Day", x=100, y=180),  # Lynna, Sunday
    ]
    result = find_open_shift_pickups(
        elements, marker_keywords=["open shift approved"], known_employee_names=KNOWN_NAMES,
        week_anchor=WEEK_ANCHOR,
    )
    assert result == [{"employee_name": "Paula Cruz-Bonde", "shift_date": date(2026, 8, 31)}]


def test_no_markers_is_empty_not_an_error():
    elements = [*DAY_HEADERS, *EMPLOYEE_ROWS, PositionedText("Unavailable All Day", x=100, y=100)]
    assert find_open_shift_pickups(
        elements, ["open shift approved"], KNOWN_NAMES, WEEK_ANCHOR
    ) == []


def test_deduplicates_same_employee_and_date():
    elements = [
        *DAY_HEADERS,
        *EMPLOYEE_ROWS,
        PositionedText("Open Shift approved", x=198, y=418),
        PositionedText("Open Shift approved badge", x=202, y=419),  # same cell, e.g. icon + label
    ]
    result = find_open_shift_pickups(elements, ["open shift approved"], KNOWN_NAMES, WEEK_ANCHOR)
    assert len(result) == 1


def test_unknown_employee_name_is_ignored_for_row_matching():
    elements = [
        *DAY_HEADERS,
        PositionedText("Someone New", x=20, y=418),  # not in our known employee list
        PositionedText("Open Shift approved", x=198, y=418),
    ]
    assert find_open_shift_pickups(elements, ["open shift approved"], KNOWN_NAMES, WEEK_ANCHOR) == []
