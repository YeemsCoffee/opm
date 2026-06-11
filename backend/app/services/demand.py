"""Demand forecast: average tickets per hour of day from historical data.
Used to weight shifts so the strongest team lands on the busiest windows."""

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Shift, Ticket


def hourly_averages(db: Session) -> dict[int, float]:
    rows = db.execute(select(Ticket.created_at).where(Ticket.recalled.is_(False))).all()
    if not rows:
        return {}
    by_hour: dict[int, int] = defaultdict(int)
    days: set[date] = set()
    for (created,) in rows:
        by_hour[created.hour] += 1
        days.add(created.date())
    return {h: n / len(days) for h, n in by_hour.items()}


def shift_demand_weight(shift: Shift, hourly: dict[int, float]) -> int:
    """Expected ticket count over the shift window, as a small integer weight.
    Falls back to weighting by duration when there is no ticket history."""
    if not hourly:
        return max(1, (shift.end_min - shift.start_min) // 60)
    expected = 0.0
    for h in range(24):
        overlap = max(0, min(shift.end_min, (h + 1) * 60) - max(shift.start_min, h * 60))
        if overlap:
            expected += hourly.get(h, 0.0) * overlap / 60
    return max(1, round(expected))
