from datetime import date, timedelta

from sqlalchemy import select

from app.models import HomebaseSyncStatus, HoursSnapshot, ShiftSwap
from app.services import homebase_browser
from tests.test_homebase_parser import HOURS_HTML

NO_MATCHING_TABLE_HTML = "<html><body><p>nothing recognizable here</p></body></html>"


class _FakePage:
    def __init__(self):
        self.url = ""

    def goto(self, url):
        self.url = url

    def wait_for_load_state(self, _state):
        pass

    def content(self):
        return HOURS_HTML if "timesheet" in self.url else NO_MATCHING_TABLE_HTML


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]
        self.closed = False

    def close(self):
        self.closed = True


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


def _install_fake_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(homebase_browser, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(homebase_browser, "sync_playwright", lambda: _FakeSyncPlaywright(_FakeContext()))


def test_swaps_failure_does_not_discard_hours(db, monkeypatch, tmp_path):
    _install_fake_browser(monkeypatch, tmp_path)

    result = homebase_browser.sync_once(db)

    assert result["hours_rows"] == 2  # Allen Tran, Megan Lee (Enoch is a no-show, skipped)
    assert result["swap_rows"] == 0
    assert result["ok"] is False  # the swaps side genuinely failed
    assert "swaps:" in result["error"]
    assert "calibration" in result["error"]

    saved = db.scalars(select(HoursSnapshot)).all()
    assert {s.employee_name for s in saved} == {"Allen Tran", "Megan Lee"}
    assert db.scalars(select(ShiftSwap)).all() == []

    status = db.scalar(select(HomebaseSyncStatus))
    assert status.session_valid is True  # a bad page, not a logged-out session
    assert status.hours_rows_last_sync == 2
    assert status.swaps_rows_last_sync == 0
    assert status.last_success_at is not None


def test_sync_upserts_on_rerun(db, monkeypatch, tmp_path):
    _install_fake_browser(monkeypatch, tmp_path)
    homebase_browser.sync_once(db)
    homebase_browser.sync_once(db)
    # same employees/period re-synced -> updated in place, not duplicated
    assert len(db.scalars(select(HoursSnapshot)).all()) == 2


def test_no_profile_yet_reports_clearly(db, monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(homebase_browser, "PROFILE_DIR", missing)
    result = homebase_browser.sync_once(db)
    assert result["ok"] is False
    assert "login script" in result["error"]
