"""CME equity-index trading calendar (rule-based approximation).

Computes full closures and early closes (13:00 New York) for any year from
the published CME equity-index holiday pattern.  Odd one-off exchange
decisions can be patched via ``extra_closures`` / ``extra_early_closes``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """US-market observation: Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@dataclass
class TradingCalendar:
    """Holidays / early closes for CME equity-index futures, NY time.

    ``is_closed`` refers to the *cash-session date*: on a "closed" date the
    9:30–16:00 equity session does not happen (Globex may trade a shortened
    holiday session; those bars are excluded from research by default).
    """

    extra_closures: set[date] = field(default_factory=set)
    extra_early_closes: set[date] = field(default_factory=set)
    _cache: dict[int, tuple[set[date], set[date]]] = field(default_factory=dict, repr=False)

    def _year_rules(self, year: int) -> tuple[set[date], set[date]]:
        if year in self._cache:
            return self._cache[year]
        closures = {
            _observed(date(year, 1, 1)),                    # New Year's Day
            _nth_weekday(year, 1, 0, 3),                    # MLK Day
            _nth_weekday(year, 2, 0, 3),                    # Presidents' Day
            _easter(year) - timedelta(days=2),              # Good Friday
            _last_weekday(year, 5, 0),                      # Memorial Day
            _observed(date(year, 6, 19)) if year >= 2022 else None,  # Juneteenth
            _observed(date(year, 7, 4)),                    # Independence Day
            _nth_weekday(year, 9, 0, 1),                    # Labor Day
            _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
            _observed(date(year, 12, 25)),                  # Christmas
        }
        closures.discard(None)
        early: set[date] = set()
        # day after Thanksgiving closes early
        early.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))
        # July 3rd when the 4th is a weekday (and the 3rd is one too)
        jul3 = date(year, 7, 3)
        if date(year, 7, 4).weekday() < 5 and jul3.weekday() < 5:
            early.add(jul3)
        # Christmas Eve on a weekday
        dec24 = date(year, 12, 24)
        if dec24.weekday() < 5 and dec24 not in closures:
            early.add(dec24)
        early -= closures
        self._cache[year] = (closures, early)
        return closures, early

    def is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5

    def is_closed(self, d: date) -> bool:
        if self.is_weekend(d):
            return True
        closures, _ = self._year_rules(d.year)
        return d in closures or d in self.extra_closures

    def is_early_close(self, d: date) -> bool:
        _, early = self._year_rules(d.year)
        return (d in early or d in self.extra_early_closes) and not self.is_closed(d)

    def is_trading_day(self, d: date) -> bool:
        return not self.is_closed(d)

    def cash_close_time(self, d: date, normal: str = "16:00", early: str = "13:00") -> str:
        return early if self.is_early_close(d) else normal

    def trading_days(self, start: date, end: date) -> list[date]:
        out = []
        d = start
        while d <= end:
            if self.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def next_trading_day(self, d: date) -> date:
        n = d + timedelta(days=1)
        while not self.is_trading_day(n):
            n += timedelta(days=1)
        return n

    def prev_trading_day(self, d: date) -> date:
        p = d - timedelta(days=1)
        while not self.is_trading_day(p):
            p -= timedelta(days=1)
        return p
