"""CP-SAT shift scheduler.

Hard constraints: availability, time off, exact level match, no overlapping
shifts, minimum rest between shifts on different days, weekly max hours.
Requirements may be underfilled — unfilled slots are returned for the manager
to resolve (the solver never substitutes levels on its own).

Objective, in strict priority order by weight:
  1. fill as many requirement slots as possible
  2. maximize team +/- weighted by each shift's forecast demand
  3. keep hours close to each employee's target (fairness)
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from ortools.sat.python import cp_model
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Assignment, Employee, Schedule, Shift, SolverConfig
from .demand import hourly_averages, shift_demand_weight
from .ratings import compute_ratings

FILL_WEIGHT = 1_000_000
FAIRNESS_WEIGHT = 1
SOLVER_TIME_LIMIT_S = 15


@dataclass
class SolveResult:
    assignments: list[tuple[int, int, int]] = field(default_factory=list)  # (emp, shift, level)
    unfilled: list[dict] = field(default_factory=list)


def _abs_minutes(week_start: date, d: date, minute: int) -> int:
    return (d - week_start).days * 1440 + minute


def is_available(emp: Employee, shift: Shift) -> bool:
    """No availability rows means 'fully available' (unconfirmed)."""
    for to in emp.time_off:
        if to.start_date <= shift.date <= to.end_date:
            return False
    windows = sorted(
        (a.start_min, a.end_min) for a in emp.availability if a.weekday == shift.date.weekday()
    )
    if not emp.availability:
        return True
    # merge windows, then check coverage
    merged: list[list[int]] = []
    for s, e in windows:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return any(s <= shift.start_min and shift.end_min <= e for s, e in merged)


def _conflicts(week_start: date, s1: Shift, s2: Shift, rest_min: int) -> bool:
    pad = rest_min if s1.date != s2.date else 0
    a1, b1 = _abs_minutes(week_start, s1.date, s1.start_min), _abs_minutes(week_start, s1.date, s1.end_min)
    a2, b2 = _abs_minutes(week_start, s2.date, s2.start_min), _abs_minutes(week_start, s2.date, s2.end_min)
    return a2 < b1 + pad and a1 < b2 + pad


def solve_schedule(db: Session, schedule: Schedule) -> SolveResult:
    week_start = schedule.week_start
    cfg = db.scalar(select(SolverConfig))
    rest_min = cfg.min_rest_minutes if cfg else 480
    lookback = cfg.rating_lookback_days if cfg else 90

    shifts = list(
        db.scalars(
            select(Shift)
            .options(selectinload(Shift.requirements))
            .where(Shift.date >= week_start, Shift.date < week_start + timedelta(days=7))
        )
    )
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
    manual = [a for a in schedule.assignments if a.manual]

    ratings = {
        r["employee_id"]: r["plus_minus"]
        for r in compute_ratings(db, week_start - timedelta(days=lookback), week_start)
    }
    hourly = hourly_averages(db)
    demand = {s.id: shift_demand_weight(s, hourly) for s in shifts}
    shift_by_id = {s.id: s for s in shifts}

    emp_level = {e.id: e.level_on(week_start) for e in employees}
    manual_by_shift_level: dict[tuple[int, int], int] = {}
    manual_minutes: dict[int, int] = {}
    for a in manual:
        if a.shift_id not in shift_by_id:
            continue
        key = (a.shift_id, a.fills_level_id)
        manual_by_shift_level[key] = manual_by_shift_level.get(key, 0) + 1
        sh = shift_by_id[a.shift_id]
        manual_minutes[a.employee_id] = manual_minutes.get(a.employee_id, 0) + (sh.end_min - sh.start_min)

    model = cp_model.CpModel()
    x: dict[tuple[int, int], cp_model.IntVar] = {}
    for e in employees:
        lvl = emp_level[e.id]
        if lvl is None:
            continue
        manual_shift_ids = {a.shift_id for a in manual if a.employee_id == e.id}
        for s in shifts:
            if s.id in manual_shift_ids:
                continue
            if not any(r.level_id == lvl.id and r.count > 0 for r in s.requirements):
                continue
            if not is_available(e, s):
                continue
            # never auto-assign on top of a conflicting manual assignment
            if any(
                _conflicts(week_start, shift_by_id[mid], s, rest_min)
                for mid in manual_shift_ids
                if mid in shift_by_id
            ):
                continue
            x[(e.id, s.id)] = model.new_bool_var(f"x_{e.id}_{s.id}")

    # requirement caps (leave room taken by manual fills)
    for s in shifts:
        for r in s.requirements:
            cap = max(0, r.count - manual_by_shift_level.get((s.id, r.level_id), 0))
            vars_ = [
                x[(e.id, s.id)]
                for e in employees
                if (e.id, s.id) in x and emp_level[e.id] and emp_level[e.id].id == r.level_id
            ]
            if vars_:
                model.add(sum(vars_) <= cap)

    # no overlap / min rest, weekly hours, fairness
    fairness_terms = []
    for e in employees:
        my = [(s, x[(e.id, s.id)]) for s in shifts if (e.id, s.id) in x]
        for i in range(len(my)):
            for j in range(i + 1, len(my)):
                if _conflicts(week_start, my[i][0], my[j][0], rest_min):
                    model.add(my[i][1] + my[j][1] <= 1)
        base = manual_minutes.get(e.id, 0)
        total = base + sum((s.end_min - s.start_min) * v for s, v in my)
        model.add(total <= max(e.max_week_minutes, base))
        if e.target_week_minutes:
            dev = model.new_int_var(0, 7 * 1440, f"dev_{e.id}")
            model.add(dev >= total - e.target_week_minutes)
            model.add(dev >= e.target_week_minutes - total)
            fairness_terms.append(dev)

    quality = []
    for (emp_id, shift_id), v in x.items():
        score = int(ratings.get(emp_id, 0.0) * 10) * demand[shift_id]
        quality.append(score * v)

    model.maximize(
        FILL_WEIGHT * sum(x.values())
        + sum(quality)
        - FAIRNESS_WEIGHT * sum(fairness_terms)
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT_S
    status = solver.solve(model)

    result = SolveResult()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for (emp_id, shift_id), v in x.items():
            if solver.value(v):
                result.assignments.append((emp_id, shift_id, emp_level[emp_id].id))

    filled: dict[tuple[int, int], int] = {}
    for _, shift_id, level_id in result.assignments:
        filled[(shift_id, level_id)] = filled.get((shift_id, level_id), 0) + 1
    for s in shifts:
        for r in s.requirements:
            have = filled.get((s.id, r.level_id), 0) + manual_by_shift_level.get((s.id, r.level_id), 0)
            if have < r.count:
                result.unfilled.append(
                    {
                        "shift_id": s.id,
                        "level_id": r.level_id,
                        "level_name": r.level.name,
                        "missing": r.count - have,
                    }
                )
    return result


def apply_solution(db: Session, schedule: Schedule, result: SolveResult) -> None:
    """Replace solver-generated assignments; manual ones are untouched."""
    for a in list(schedule.assignments):
        if not a.manual:
            db.delete(a)
    db.flush()
    for emp_id, shift_id, level_id in result.assignments:
        db.add(
            Assignment(
                schedule_id=schedule.id,
                shift_id=shift_id,
                employee_id=emp_id,
                fills_level_id=level_id,
                manual=False,
            )
        )
    db.commit()
