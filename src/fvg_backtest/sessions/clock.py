"""New York session clock.

All strategy decisions run in ``America/New_York``.  A Globex session starts
at 18:00 NY the *previous* calendar day and is named after the cash date it
precedes (its ``globex_session_date``): the bar stamped Tuesday 18:30 NY
belongs to Wednesday's session.  DST is handled by real timezone conversion
(zoneinfo), never by fixed offsets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from ..config.schema import SessionConfig
from .calendar import TradingCalendar

NY_TZ = ZoneInfo("America/New_York")


class SessionSegment(StrEnum):
    OVERNIGHT = "OVERNIGHT"          # 18:00 prev day -> premarket_start
    PREMARKET = "PREMARKET"          # premarket_start -> cash_open
    CASH = "CASH"                    # cash_open -> cash_close
    POST_CASH = "POST_CASH"          # cash_close -> globex_session_end (17:00)
    MAINTENANCE = "MAINTENANCE"      # 17:00 -> 18:00
    CLOSED = "CLOSED"                # weekend / holiday dead zone


def hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _t(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


@dataclass
class SessionClock:
    config: SessionConfig
    calendar: TradingCalendar

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.config.timezone)

    # -- conversions --------------------------------------------------------

    def to_ny(self, ts_utc: datetime) -> datetime:
        if ts_utc.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware UTC")
        return ts_utc.astimezone(self.tz)

    def ny_datetime(self, d: date, hhmm: str) -> datetime:
        """Wall-clock NY datetime for a session date (DST-safe)."""
        return datetime.combine(d, _t(hhmm), tzinfo=self.tz)

    # -- session assignment --------------------------------------------------

    def globex_session_date(self, ts: datetime) -> date:
        """Cash date whose Globex session contains ``ts`` (NY or UTC aware).

        Bars at/after globex_session_start roll to the next calendar day;
        weekend rolls land on Monday (Sunday 18:00 open).  Saturday bars
        (shouldn't exist) also map forward to Monday.
        """
        ny = ts.astimezone(self.tz)
        d = ny.date()
        if ny.time() >= _t(self.config.globex_session_start):
            d = d + timedelta(days=1)
        while d.weekday() >= 5:  # Sat/Sun -> Monday
            d += timedelta(days=1)
        return d

    def segment(self, ts: datetime) -> SessionSegment:
        ny = ts.astimezone(self.tz)
        t = ny.time()
        cfg = self.config
        if ny.weekday() == 5:  # Saturday
            return SessionSegment.CLOSED
        if ny.weekday() == 6 and t < _t(cfg.globex_session_start):  # Sunday pre-open
            return SessionSegment.CLOSED
        if ny.weekday() == 4 and t >= _t(cfg.globex_session_start):  # Friday evening
            return SessionSegment.CLOSED
        if _t(cfg.maintenance_break_start) <= t < _t(cfg.maintenance_break_end):
            return SessionSegment.MAINTENANCE
        if t >= _t(cfg.globex_session_start) or t < _t(cfg.premarket_start):
            return SessionSegment.OVERNIGHT
        if t < _t(cfg.cash_open):
            return SessionSegment.PREMARKET
        close = cfg.cash_close
        session_date = self.globex_session_date(ts)
        if self.calendar.is_early_close(session_date):
            close = self.calendar.cash_close_time(session_date, normal=cfg.cash_close)
        if t < _t(close):
            return SessionSegment.CASH
        return SessionSegment.POST_CASH

    # -- boundaries for one session date ------------------------------------

    def session_bounds(self, session_date: date) -> tuple[datetime, datetime]:
        """(globex_open, globex_close) NY datetimes for a cash date.

        Open is 18:00 the previous *calendar* day (Sunday for Monday
        sessions); close is 17:00 on the session date, or the early-close
        cash close on early-close days.
        """
        # Monday sessions open Sunday 18:00 — still the previous calendar day
        prev = session_date - timedelta(days=1)
        open_dt = self.ny_datetime(prev, self.config.globex_session_start)
        close_hhmm = self.config.globex_session_end
        if self.calendar.is_early_close(session_date):
            close_hhmm = self.calendar.cash_close_time(
                session_date, normal=self.config.globex_session_end
            )
        close_dt = self.ny_datetime(session_date, close_hhmm)
        return open_dt, close_dt

    def cash_open_dt(self, session_date: date) -> datetime:
        return self.ny_datetime(session_date, self.config.cash_open)

    def cash_close_dt(self, session_date: date) -> datetime:
        hhmm = self.calendar.cash_close_time(session_date, normal=self.config.cash_close)
        return self.ny_datetime(session_date, hhmm)

    def fvg_search_start_dt(self, session_date: date) -> datetime:
        return self.ny_datetime(session_date, self.config.fvg_search_start)

    def fvg_search_end_dt(self, session_date: date) -> datetime:
        end = self.ny_datetime(session_date, self.config.fvg_search_end)
        return min(end, self.cash_close_dt(session_date))

    def management_end_dt(self, session_date: date) -> datetime:
        end = self.ny_datetime(session_date, self.config.trade_management_end)
        return min(end, self.cash_close_dt(session_date))
