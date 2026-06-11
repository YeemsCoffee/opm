"""CP-SAT shift scheduler.

Hard constraints: availability, time off, exact level match, no overlapping
shifts, minimum rest between shifts on different days, daily and weekly hour
caps, and no 7th consecutive worked day (rolling across week boundaries,
seeded from timesheets and previously generated schedules). Requirements may
be underfilled — unfilled slots are returned for the manager to resolve (the
solver never substitutes levels or breaks limits on its own; manual overrides
can, and are flagged as warnings).

Objective, in strict priority order by weight:
  1. fill as many requirement slots as possible
  2. maximize team +/- weighted by each shift's forecast demand
  3. keep hours close to each employee's target (fairness)
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from ortools.sat.python import cp_model
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Assignment, Employee, Schedule, Shift, SolverConfig, WorkSession
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


def worked_dates_before(db: Session, start: date, days: int) -> dict[int, set[date]]:
    """Dates each employee worked in the `days` days before `start`, from
    timesheet sessions and from assignments in other schedules. Seeds the
    rolling consecutive-days rule across week boundaries."""
    lo = start - timedelta(days=days)
    out: dict[int, set[date]] = defaultdict(set)
    for emp_id, clock_in in db.execute(
        select(WorkSession.employee_id, WorkSession.clock_in).where(
            WorkSession.clock_in >= lo, WorkSession.clock_in < start
        )
    ):
        out[emp_id].add(clock_in.date())
    for emp_id, shift_date in db.execute(
        select(Assignment.employee_id, Shift.date)
        .join(Shift, Assignment.shift_id == Shift.id)
        .where(Shift.date >= lo, Shift.date < start)
    ):
        out[emp_id].add(shift_date)
    return out


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
    max_day = cfg.max_day_minutes if cfg else 480
    max_consec = cfg.max_consecutive_days if cfg else 6

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
    history = worked_dates_before(db, week_start, max_consec)
    manual_by_shift_level: dict[tuple[int, int], int] = {}
    manual_minutes: dict[int, int] = {}
    manual_day_minutes: dict[tuple[int, date], int] = defaultdict(int)
    manual_days: dict[int, set[date]] = defaultdict(set)
    for a in manual:
        if a.shift_id not in shift_by_id:
            continue
        key = (a.shift_id, a.fills_level_id)
        manual_by_shift_level[key] = manual_by_shift_level.get(key, 0) + 1
        sh = shift_by_id[a.shift_id]
        manual_minutes[a.employee_id] = manual_minutes.get(a.employee_id, 0) + (sh.end_min - sh.start_min)
        manual_day_minutes[(a.employee_id, sh.date)] += sh.end_min - sh.start_min
        manual_days[a.employee_id].add(sh.date)

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

    # no overlap / min rest, daily + weekly hours, consecutive days, fairness
    fairness_terms = []
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
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

        # daily cap (manual overrides may exceed it; autos never do)
        for d in week_dates:
            day_shifts = [(s, v) for s, v in my if s.date == d]
            manual_today = manual_day_minutes.get((e.id, d), 0)
            if day_shifts:
                model.add(
                    manual_today + sum((s.end_min - s.start_min) * v for s, v in day_shifts)
                    <= max(max_day, manual_today)
                )

        # rolling consecutive-days limit: in any (max_consec + 1)-day window,
        # at most max_consec worked days. Past days come from timesheets and
        # earlier schedules; manual assignments count as constants.
        worked: dict[date, object] = {}
        for d in week_dates:
            day_vars = [v for s, v in my if s.date == d]
            if d in manual_days[e.id]:
                worked[d] = 1
            elif day_vars:
                dv = model.new_bool_var(f"day_{e.id}_{d}")
                for v in day_vars:
                    model.add(dv >= v)
                worked[d] = dv
            else:
                worked[d] = 0
        past = history.get(e.id, set())
        for offset in range(-max_consec, 7 - max_consec):
            window = [week_start + timedelta(days=offset + k) for k in range(max_consec + 1)]
            terms = [worked[d] if d >= week_start else int(d in past) for d in window]
            if any(not isinstance(t, int) for t in terms):
                model.add(sum(terms) <= max_consec)

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


def streak_with(dates: set[date], d: date) -> int:
    """Length of the consecutive-day run through `d` if `d` is also worked."""
    worked = set(dates) | {d}
    run = 1
    cur = d - timedelta(days=1)
    while cur in worked:
        run += 1
        cur -= timedelta(days=1)
    cur = d + timedelta(days=1)
    while cur in worked:
        run += 1
        cur += timedelta(days=1)
    return run


def schedule_warnings(db: Session, schedule: Schedule, shifts: list[Shift]) -> list[dict]:
    """Flags for overridable limits the current assignments cross: >max h/day,
    > weekly cap, and 7+ consecutive days (rolling, seeded from history).
    The solver never creates these on its own — they come from manual picks."""
    cfg = db.scalar(select(SolverConfig))
    max_day = cfg.max_day_minutes if cfg else 480
    max_consec = cfg.max_consecutive_days if cfg else 6
    week_start = schedule.week_start
    shift_by_id = {s.id: s for s in shifts}

    day_minutes: dict[int, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    dates_worked: dict[int, set[date]] = defaultdict(set)
    names: dict[int, str] = {}
    for a in schedule.assignments:
        sh = shift_by_id.get(a.shift_id)
        if sh is None:
            continue
        day_minutes[a.employee_id][sh.date] += sh.end_min - sh.start_min
        dates_worked[a.employee_id].add(sh.date)
        names[a.employee_id] = a.employee.name

    history = worked_dates_before(db, week_start, max_consec)
    emp_caps = {
        e.id: e.max_week_minutes
        for e in db.scalars(select(Employee).where(Employee.id.in_(names.keys())))
    }

    warnings = []
    for emp_id, by_day in day_minutes.items():
        for d, mins in sorted(by_day.items()):
            if mins > max_day:
                warnings.append(
                    {
                        "employee_id": emp_id,
                        "employee_name": names[emp_id],
                        "kind": "overtime_day",
                        "message": f"{names[emp_id]}: {mins / 60:.1f}h on {d} (over {max_day / 60:.0f}h/day)",
                    }
                )
        total = sum(by_day.values())
        cap = emp_caps.get(emp_id, 2400)
        if total > cap:
            warnings.append(
                {
                    "employee_id": emp_id,
                    "employee_name": names[emp_id],
                    "kind": "overtime_week",
                    "message": f"{names[emp_id]}: {total / 60:.1f}h this week (over their {cap / 60:.0f}h cap)",
                }
            )
        all_dates = dates_worked[emp_id] | history.get(emp_id, set())
        flagged_runs = set()
        for d in sorted(dates_worked[emp_id]):
            run = streak_with(all_dates - {d}, d)
            if run > max_consec:
                run_start = d
                while run_start - timedelta(days=1) in all_dates:
                    run_start -= timedelta(days=1)
                if run_start in flagged_runs:
                    continue
                flagged_runs.add(run_start)
                warnings.append(
                    {
                        "employee_id": emp_id,
                        "employee_name": names[emp_id],
                        "kind": "consecutive_days",
                        "message": f"{names[emp_id]}: {run} consecutive days (run starting {run_start})",
                    }
                )
    return warnings


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
