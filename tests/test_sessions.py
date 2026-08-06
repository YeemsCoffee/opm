from __future__ import annotations

from datetime import date, datetime, timezone

from fvg_backtest.sessions import SessionClock, SessionSegment, TradingCalendar

UTC = timezone.utc


def test_holidays_2025(calendar: TradingCalendar):
    assert calendar.is_closed(date(2025, 1, 1))       # New Year
    assert calendar.is_closed(date(2025, 1, 20))      # MLK
    assert calendar.is_closed(date(2025, 2, 17))      # Presidents
    assert calendar.is_closed(date(2025, 4, 18))      # Good Friday
    assert calendar.is_closed(date(2025, 5, 26))      # Memorial
    assert calendar.is_closed(date(2025, 6, 19))      # Juneteenth
    assert calendar.is_closed(date(2025, 7, 4))       # July 4
    assert calendar.is_closed(date(2025, 9, 1))       # Labor
    assert calendar.is_closed(date(2025, 11, 27))     # Thanksgiving
    assert calendar.is_closed(date(2025, 12, 25))     # Christmas
    assert calendar.is_closed(date(2025, 1, 4))       # Saturday
    assert not calendar.is_closed(date(2025, 1, 6))


def test_early_closes(calendar: TradingCalendar):
    assert calendar.is_early_close(date(2025, 11, 28))  # day after Thanksgiving
    assert calendar.is_early_close(date(2025, 12, 24))  # Christmas Eve (Wed)
    assert calendar.is_early_close(date(2025, 7, 3))    # July 3 (Thu before a Friday July 4)
    assert calendar.cash_close_time(date(2025, 11, 28)) == "13:00"
    assert calendar.cash_close_time(date(2025, 1, 6)) == "16:00"


def test_good_friday_2024(calendar: TradingCalendar):
    assert calendar.is_closed(date(2024, 3, 29))


def test_globex_session_date_assignment(clock: SessionClock):
    ny = clock.tz
    # Tuesday 18:30 NY belongs to Wednesday's session
    assert clock.globex_session_date(datetime(2025, 1, 7, 18, 30, tzinfo=ny)) == date(2025, 1, 8)
    # Tuesday 15:00 NY belongs to Tuesday
    assert clock.globex_session_date(datetime(2025, 1, 7, 15, 0, tzinfo=ny)) == date(2025, 1, 7)
    # Sunday 19:00 NY belongs to Monday
    assert clock.globex_session_date(datetime(2025, 1, 5, 19, 0, tzinfo=ny)) == date(2025, 1, 6)
    # works from UTC input too: 2025-01-08 00:30 UTC == Tue 19:30 NY -> Wednesday
    assert clock.globex_session_date(datetime(2025, 1, 8, 0, 30, tzinfo=UTC)) == date(2025, 1, 8)


def test_segments(clock: SessionClock):
    ny = clock.tz
    mk = lambda h, m: datetime(2025, 1, 7, h, m, tzinfo=ny)  # a Tuesday
    assert clock.segment(mk(3, 59)) == SessionSegment.OVERNIGHT
    assert clock.segment(mk(4, 0)) == SessionSegment.PREMARKET
    assert clock.segment(mk(9, 29)) == SessionSegment.PREMARKET
    assert clock.segment(mk(9, 30)) == SessionSegment.CASH
    assert clock.segment(mk(15, 59)) == SessionSegment.CASH
    assert clock.segment(mk(16, 0)) == SessionSegment.POST_CASH
    assert clock.segment(mk(17, 30)) == SessionSegment.MAINTENANCE
    assert clock.segment(mk(18, 0)) == SessionSegment.OVERNIGHT
    # Saturday is closed
    assert clock.segment(datetime(2025, 1, 4, 12, 0, tzinfo=ny)) == SessionSegment.CLOSED
    # Friday evening after 18:00 is closed (no Friday-night Globex)
    assert clock.segment(datetime(2025, 1, 3, 19, 0, tzinfo=ny)) == SessionSegment.CLOSED


def test_daylight_saving_transition(clock: SessionClock):
    # US DST began 2025-03-09: 9:30 NY was 14:30 UTC before, 13:30 UTC after
    before = clock.cash_open_dt(date(2025, 3, 7)).astimezone(UTC)
    after = clock.cash_open_dt(date(2025, 3, 10)).astimezone(UTC)
    assert (before.hour, before.minute) == (14, 30)
    assert (after.hour, after.minute) == (13, 30)
    # fall back 2025-11-02
    before = clock.cash_open_dt(date(2025, 10, 31)).astimezone(UTC)
    after = clock.cash_open_dt(date(2025, 11, 3)).astimezone(UTC)
    assert before.hour == 13 and after.hour == 14


def test_session_bounds_and_early_close(clock: SessionClock):
    open_dt, close_dt = clock.session_bounds(date(2025, 1, 6))  # Monday
    assert open_dt == clock.ny_datetime(date(2025, 1, 5), "18:00")  # Sunday evening
    assert close_dt == clock.ny_datetime(date(2025, 1, 6), "17:00")
    # early close: day after Thanksgiving ends at 13:00
    _, close_dt = clock.session_bounds(date(2025, 11, 28))
    assert close_dt == clock.ny_datetime(date(2025, 11, 28), "13:00")
    assert clock.cash_close_dt(date(2025, 11, 28)).hour == 13
