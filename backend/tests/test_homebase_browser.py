from datetime import date, timedelta

from sqlalchemy import select

from app.models import Employee, HomebaseSyncStatus, HoursSnapshot, ShiftSwap
from app.services import homebase_browser
from app.services.homebase_grid_parser import week_start_for
from tests.test_homebase_parser import HOURS_HTML

WEEK_START = week_start_for(date.today())
_WEEKDAY_ABBR = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DAY_HEADERS = [
    {"text": f"{_WEEKDAY_ABBR[i]}, {i + 1}", "x": 100 + i * 100, "y": 10} for i in range(7)
]  # header text just needs to match the day-header pattern; POSITION (index) drives the
# date attribution in find_open_shift_pickups, not the day number written here
GRID_WITH_PICKUP = DAY_HEADERS + [
    {"text": "Allen Tran", "x": 20, "y": 100},
    {"text": "Megan Lee", "x": 20, "y": 200},
    {"text": "6:30am-11:30am K1 Open Shift approved", "x": 205, "y": 202},  # Megan, day index 1
]


class _FakePage:
    def __init__(self, grid_data, evaluate_error=None):
        self.url = ""
        self._grid_data = grid_data
        self._evaluate_error = evaluate_error

    def goto(self, url):
        self.url = url

    def wait_for_load_state(self, _state):
        pass

    def content(self):
        return HOURS_HTML

    def evaluate(self, _js):
        if self._evaluate_error:
            raise self._evaluate_error
        return self._grid_data


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]

    def close(self):
        pass


class _FakeChromium:
    def __init__(self, context):
        self._context = context

    def launch_persistent_context(self, _profile_dir, headless):
        return self._context


class _FakePlaywright:
    def __init__(self, context):
        self.chromium = _FakeChromium(context)


class _FakeSyncPlaywright:
    def __init__(self, context):
        self._context = context

    def __enter__(self):
        return _FakePlaywright(self._context)

    def __exit__(self, *_a):
        return False


def _install_fake_browser(monkeypatch, tmp_path, page):
    monkeypatch.setattr(homebase_browser, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(homebase_browser, "sync_playwright", lambda: _FakeSyncPlaywright(_FakeContext(page)))


def _seed_employees(db):
    db.add(Employee(name="Allen Tran"))
    db.add(Employee(name="Megan Lee"))
    db.commit()


def test_hours_and_swaps_both_succeed(db, monkeypatch, tmp_path):
    _seed_employees(db)
    _install_fake_browser(monkeypatch, tmp_path, _FakePage(GRID_WITH_PICKUP))

    result = homebase_browser.sync_once(db)

    assert result["ok"] is True
    assert result["hours_rows"] == 2  # Allen Tran, Megan Lee (Enoch is a no-show, skipped)
    assert result["swap_rows"] == 1

    swap = db.scalar(select(ShiftSwap))
    assert swap.covered_by == "Megan Lee"
    assert swap.shift_date == WEEK_START + timedelta(days=1)
    assert swap.status == "Open Shift approved"

    status = db.scalar(select(HomebaseSyncStatus))
    assert status.session_valid is True
    assert status.hours_rows_last_sync == 2
    assert status.swaps_rows_last_sync == 1
    assert status.last_error == ""


def test_swaps_failure_does_not_discard_hours(db, monkeypatch, tmp_path):
    _seed_employees(db)
    _install_fake_browser(monkeypatch, tmp_path, _FakePage(None, evaluate_error=RuntimeError("page crashed")))

    result = homebase_browser.sync_once(db)

    assert result["hours_rows"] == 2
    assert result["swap_rows"] == 0
    assert result["ok"] is False  # the swaps side genuinely failed
    assert "swaps: page crashed" in result["error"]

    saved = db.scalars(select(HoursSnapshot)).all()
    assert {s.employee_name for s in saved} == {"Allen Tran", "Megan Lee"}
    assert db.scalars(select(ShiftSwap)).all() == []

    status = db.scalar(select(HomebaseSyncStatus))
    assert status.session_valid is True  # a scrape error, not a logged-out session
    assert status.hours_rows_last_sync == 2
    assert status.swaps_rows_last_sync == 0
    assert status.last_success_at is not None


def test_empty_swaps_result_is_not_a_failure(db, monkeypatch, tmp_path):
    """A week with zero shift pickups is a legitimate result, not an error."""
    _seed_employees(db)
    _install_fake_browser(monkeypatch, tmp_path, _FakePage(DAY_HEADERS))  # no marker present

    result = homebase_browser.sync_once(db)
    assert result["ok"] is True
    assert result["swap_rows"] == 0


def test_sync_upserts_on_rerun(db, monkeypatch, tmp_path):
    _seed_employees(db)
    _install_fake_browser(monkeypatch, tmp_path, _FakePage(GRID_WITH_PICKUP))
    homebase_browser.sync_once(db)
    homebase_browser.sync_once(db)
    # same employees/period/swap re-synced -> updated in place, not duplicated
    assert len(db.scalars(select(HoursSnapshot)).all()) == 2
    assert len(db.scalars(select(ShiftSwap)).all()) == 1


def test_no_profile_yet_reports_clearly(db, monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(homebase_browser, "PROFILE_DIR", missing)
    result = homebase_browser.sync_once(db)
    assert result["ok"] is False
    assert "login script" in result["error"]
