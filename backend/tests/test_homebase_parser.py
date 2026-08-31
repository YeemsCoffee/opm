from app.services.homebase_table_parser import extract_hours_total, parse_hours_table, parse_swaps_table

# Structure matches a real Homebase Timesheets screenshot: Team member /
# Worked / Total paid breaks / Total unpaid breaks / Issues / Actions.
# No-show rows have no "Total:" line at all.
HOURS_HTML = """
<table>
  <tr><th>Team member</th><th>Worked</th><th>Total paid breaks</th><th>Total unpaid breaks</th><th>Issues</th><th>Actions</th></tr>
  <tr>
    <td>Allen Tran<br>B2</td>
    <td>9:28 am - 5:48 pm<br>Total: 8 hrs 20 min<br>20 min over scheduled time</td>
    <td>20 min</td><td>30 min</td><td></td><td></td>
  </tr>
  <tr>
    <td>Enoch Chung<br>Manager</td>
    <td>- - - - - - -<br>Scheduled: 7:30 am - 9:00 am</td>
    <td>0 min</td><td>0 min</td><td>No show</td><td>Resolve issues</td>
  </tr>
  <tr>
    <td>Megan Lee<br>B2</td>
    <td>7:34 am - 1:06 pm<br>Total: 5 hrs 32 min</td>
    <td>10 min</td><td>0 min</td><td></td><td></td>
  </tr>
</table>
"""

SWAPS_HTML = """
<table>
  <tr><th>Date</th><th>Role</th><th>Released by</th><th>Covered by</th><th>Status</th></tr>
  <tr><td>08/22/2026</td><td>K1</td><td>Kyle Dador</td><td>Syd Smith</td><td>Approved</td></tr>
  <tr><td>08/23/2026</td><td>B2</td><td>Sandra Armenta</td><td></td><td>Open</td></tr>
</table>
"""


def test_extract_hours_total():
    text = "9:28 am - 5:48 pm Total: 8 hrs 20 min 20 min over scheduled time"
    assert extract_hours_total(text) == round(8 + 20 / 60, 2)
    assert extract_hours_total("Total: 5 hrs") == 5.0
    assert extract_hours_total("- - - - - - - Scheduled: 7:30 am - 9:00 am") is None


def test_parse_hours_table_skips_no_shows():
    rows = parse_hours_table(HOURS_HTML, ["team member", "name"], ["worked"])
    by_name = {r["name"]: r["hours"] for r in rows}
    assert by_name["Allen Tran"] == round(8 + 20 / 60, 2)
    assert by_name["Megan Lee"] == round(5 + 32 / 60, 2)
    assert "Enoch Chung" not in by_name


def test_parse_hours_table_bad_config_raises():
    try:
        parse_hours_table(HOURS_HTML, ["nonexistent_column"], ["also_nonexistent"])
        assert False, "should have raised"
    except ValueError as exc:
        assert "calibration" in str(exc)


def test_parse_swaps_table():
    rows = parse_swaps_table(
        SWAPS_HTML,
        date_keywords=["date"],
        released_by_keywords=["released by"],
        covered_by_keywords=["covered by"],
        role_keywords=["role"],
        status_keywords=["status"],
    )
    assert len(rows) == 1  # the open (uncovered) row is excluded
    assert rows[0]["covered_by"] == "syd smith"
    assert rows[0]["released_by"] == "kyle dador"
