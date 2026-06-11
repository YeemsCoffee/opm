"""Ranked candidate suggestions for an unfilled requirement slot.

The solver never bends rules; this is where near-miss candidates surface for
the manager, each labeled with the rule(s) that assigning them would bend.
Softness 0 is the gentlest bend, higher numbers need a conversation.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Employee, Level, Schedule, Shift, SolverConfig
from .ratings import compute_ratings
from .solver import _conflicts, is_available, streak_with, worked_dates_before


def suggest_for_slot(db: Session, schedule: Schedule, shift: Shift, level: Level) -> list[dict]:
    cfg = db.scalar(select(SolverConfig))
    rest_min = cfg.min_rest_minutes if cfg else 480
    lookback = cfg.rating_lookback_days if cfg else 90
    max_day = cfg.max_day_minutes if cfg else 480
    max_consec = cfg.max_consecutive_days if cfg else 6
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
    history = worked_dates_before(db, week_start, max_consec)
    assigned_shifts: dict[int, list[Shift]] = {}
    assigned_minutes: dict[int, int] = {}
    for a in schedule.assignments:
        assigned_shifts.setdefault(a.employee_id, []).append(a.shift)
        assigned_minutes[a.employee_id] = (
            assigned_minutes.get(a.employee_id, 0) + a.shift.end_min - a.shift.start_min
        )

    shift_minutes = shift.end_min - shift.start_min
    out = []
    for e in employees:
        lvl = e.level_on(week_start)
        if lvl is None or not lvl.counts_for_rating:
            continue
        mine = assigned_shifts.get(e.id, [])
        if any(s.id == shift.id for s in mine):
            continue
        # hard exclusions: time off, conflicts with existing assignments
        if any(to.start_date <= shift.date <= to.end_date for to in e.time_off):
            continue
        if any(_conflicts(week_start, s, shift, rest_min) for s in mine):
            continue

        same_level = lvl.id == level.id
        higher = lvl.rank > level.rank
        if not same_level and not higher:
            continue

        flags: list[tuple[int, str]] = []
        if not same_level:
            flags.append((0, "Higher level covering down"))
        if assigned_minutes.get(e.id, 0) + shift_minutes > e.max_week_minutes:
            flags.append((1, "Would exceed their weekly hours cap (overtime)"))
        day_mins = sum(s.end_min - s.start_min for s in mine if s.date == shift.date)
        if day_mins + shift_minutes > max_day:
            flags.append((1, f"Would exceed {max_day / 60:.0f}h that day (overtime)"))
        worked = {s.date for s in mine} | history.get(e.id, set())
        run = streak_with(worked, shift.date)
        if run > max_consec:
            flags.append((2, f"Would be consecutive day #{run} (limit {max_consec})"))
        if not is_available(e, shift):
            flags.append((3, "Outside their submitted availability"))

        if not flags and same_level:
            softness = -1
            reason = "Eligible now (schedule changed since last solve)"
        else:
            softness = max(f[0] for f in flags)
            reason = "; ".join(f[1] for f in flags)

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
