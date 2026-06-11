"""Ranked candidate suggestions for an unfilled requirement slot.

The solver never bends rules; this is where near-miss candidates surface for
the manager, each labeled with the rule that assigning them would bend.
Softness 0 is the gentlest bend, higher numbers need a conversation.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Employee, Level, Schedule, Shift, SolverConfig
from .ratings import compute_ratings
from .solver import _conflicts, is_available

REASONS = {
    0: "Higher level covering down",
    1: "Would exceed their weekly hours cap",
    2: "Outside their submitted availability",
    3: "Higher level, outside their availability",
}


def suggest_for_slot(db: Session, schedule: Schedule, shift: Shift, level: Level) -> list[dict]:
    cfg = db.scalar(select(SolverConfig))
    rest_min = cfg.min_rest_minutes if cfg else 480
    lookback = cfg.rating_lookback_days if cfg else 90
    week_start = schedule.week_start

    ratings = {
        r["employee_id"]: r
        for r in compute_ratings(db, week_start - timedelta(days=lookback), week_start)
    }

    employees = list(
        db.scalars(
            select(Employee)
            .options(
                selectinload(Employee.availability),
                selectinload(Employee.time_off),
                selectinload(Employee.level_history),
            )
            .where(Employee.active.is_(True))
        )
    )
    assigned_shifts: dict[int, list[Shift]] = {}
    assigned_minutes: dict[int, int] = {}
    for a in schedule.assignments:
        assigned_shifts.setdefault(a.employee_id, []).append(a.shift)
        assigned_minutes[a.employee_id] = (
            assigned_minutes.get(a.employee_id, 0) + a.shift.end_min - a.shift.start_min
        )

    out = []
    for e in employees:
        lvl = e.level_on(week_start)
        if lvl is None or not lvl.counts_for_rating:
            continue
        if any(a.id == shift.id for a in assigned_shifts.get(e.id, [])):
            continue
        # hard exclusions: time off, conflicts with existing assignments
        if any(to.start_date <= shift.date <= to.end_date for to in e.time_off):
            continue
        if any(_conflicts(week_start, s, shift, rest_min) for s in assigned_shifts.get(e.id, [])):
            continue

        same_level = lvl.id == level.id
        higher = lvl.rank > level.rank
        if not same_level and not higher:
            continue
        available = is_available(e, shift)
        over_hours = (
            assigned_minutes.get(e.id, 0) + (shift.end_min - shift.start_min)
            > e.max_week_minutes
        )

        if same_level and available and not over_hours:
            # the solver would have placed them; only possible if they became
            # free after a manual change — surface them first
            softness, reason = -1, "Eligible now (schedule changed since last solve)"
        elif higher and available and not over_hours:
            softness, reason = 0, REASONS[0]
        elif same_level and available and over_hours:
            softness, reason = 1, REASONS[1]
        elif same_level and not available:
            softness, reason = 2, REASONS[2]
        elif higher and not available:
            softness, reason = 3, REASONS[3]
        else:  # higher + over hours
            softness, reason = 3, REASONS[1]

        r = ratings.get(e.id)
        out.append(
            {
                "employee_id": e.id,
                "employee_name": e.name,
                "level_name": lvl.name,
                "rating": r["plus_minus"] if r else None,
                "tickets": r["tickets"] if r else 0,
                "reason": reason,
                "softness": softness,
            }
        )

    out.sort(key=lambda c: (c["softness"], -(c["rating"] if c["rating"] is not None else 0)))
    return out
