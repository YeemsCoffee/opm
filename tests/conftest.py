from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from fvg_backtest.config import AppConfig
from fvg_backtest.sessions import SessionClock, TradingCalendar

UTC = timezone.utc


@pytest.fixture()
def config() -> AppConfig:
    return AppConfig()


@pytest.fixture()
def calendar() -> TradingCalendar:
    return TradingCalendar()


@pytest.fixture()
def clock(config: AppConfig, calendar: TradingCalendar) -> SessionClock:
    return SessionClock(config=config.sessions, calendar=calendar)


@pytest.fixture()
def jan6() -> date:
    # Monday, 2025-01-06 — a normal full session
    return date(2025, 1, 6)


def utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)
