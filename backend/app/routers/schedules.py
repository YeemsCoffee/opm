from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import schemas
from ..auth import current_user, require_manager
from ..db import get_db
from ..models import Assignment, Employee, Level, Schedule, Shift, ShiftRequirement, User
from ..services.solver import _conflicts, apply_solution, solve_schedule
from ..services.suggestions import suggest_for_slot

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


def _load_schedule(db: Session, schedule_id: int) -> Schedule:
    schedule = db.scalar(
        select(Schedule)
        .options(
            selectinload(Schedule.assignments).selectinload(Assignment.employee),
            selectinload(Schedule.assignments).selectinload(Assignment.shift),
        )
        .where(Schedule.id == schedule_id)
        # repopulate the assignments collection even if this Schedule is
        # already in the identity map with a stale (pre-solve) collection
        .execution_options(populate_existing=True)
    )
    if schedule is None:
        raise HTTPException(404, "Schedule not found")
    return schedule


def _week_shifts(db: Session, week_start: date) -> list[Shift]:
    return list(
        db.scalars(
            select(Shift)
            .options(selectinload(Shift.requirements).selectinload(ShiftRequirement.level))
            .where(Shift.date >= week_start, Shift.date < week_start + timedelta(days=7))
            .order_by(Shift.date, Shift.start_min)
        )
    )


def _unfilled(schedule: Schedule, shifts: list[Shift]) -> list[dict]:
    counts: dict[tuple[int, int], int] = {}
    for a in schedule.assignments:
        key = (a.shift_id, a.fills_level_id)
        counts[key] = counts.get(key, 0) + 1
    out = []
    for s in shifts:
        for r in s.requirements:
            have = counts.get((s.id, r.level_id), 0)
            if have < r.count:
                out.append(
                    {
                        "shift_id": s.id,
                        "level_id": r.level_id,
                        "level_name": r.level.name,
                        "missing": r.count - have,
                    }
                )
    return out


def _detail(db: Session, schedule: Schedule) -> dict:
    shifts = _week_shifts(db, schedule.week_start)
    return {
        "schedule": {
            "id": schedule.id,
            "week_start": schedule.week_start,
            "status": schedule.status,
            "assignments": [
                {
                    "id": a.id,
                    "shift_id": a.shift_id,
                    "employee_id": a.employee_id,
                    "fills_level_id": a.fills_level_id,
                    "manual": a.manual,
                    "employee": {
                        "id": a.employee.id,
                        "name": a.employee.name,
                        "payroll_id": a.employee.payroll_id,
                        "active": a.employee.active,
                        "max_week_minutes": a.employee.max_week_minutes,
                        "target_week_minutes": a.employee.target_week_minutes,
                        "availability_confirmed": a.employee.availability_confirmed,
                        "level": a.employee.level_on(schedule.week_start),
                    },
                }
                for a in schedule.assignments
            ],
        },
        "shifts": shifts,
        "unfilled": _unfilled(schedule, shifts),
    }


@router.get("/week/{week_start}", response_model=schemas.ScheduleDetail)
def get_week(week_start: date, user: User = Depends(current_user), db: Session = Depends(get_db)):
    schedule = db.scalar(
        select(Schedule)
        .options(
            selectinload(Schedule.assignments).selectinload(Assignment.employee),
            selectinload(Schedule.assignments).selectinload(Assignment.shift),
        )
        .where(Schedule.week_start == week_start)
        .execution_options(populate_existing=True)
    )
    if schedule is None:
        raise HTTPException(404, "No schedule for that week yet")
    if user.role != "manager" and schedule.status != "published":
        raise HTTPException(403, "Schedule not published yet")
    return _detail(db, schedule)


@router.post("/generate", response_model=schemas.ScheduleDetail)
def generate(week_start: date, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    if week_start.weekday() != 0:
        raise HTTPException(422, "week_start must be a Monday")
    if not _week_shifts(db, week_start):
        raise HTTPException(422, "No shifts defined for that week — add shifts first")
    schedule = db.scalar(select(Schedule).where(Schedule.week_start == week_start))
    if schedule is None:
        schedule = Schedule(week_start=week_start)
        db.add(schedule)
        db.commit()
    schedule = _load_schedule(db, schedule.id)
    result = solve_schedule(db, schedule)
    apply_solution(db, schedule, result)
    return _detail(db, _load_schedule(db, schedule.id))


@router.post("/{schedule_id}/assignments", response_model=schemas.ScheduleDetail)
def manual_assign(
    schedule_id: int,
    body: schemas.ManualAssignIn,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    schedule = _load_schedule(db, schedule_id)
    shift = db.get(Shift, body.shift_id)
    employee = db.get(Employee, body.employee_id)
    level = db.get(Level, body.fills_level_id)
    if shift is None or employee is None or level is None:
        raise HTTPException(404, "Shift, employee or level not found")
    if not any(r.level_id == body.fills_level_id for r in shift.requirements):
        raise HTTPException(422, "Shift has no requirement for that level")
    for a in schedule.assignments:
        if a.employee_id == body.employee_id:
            if a.shift_id == body.shift_id:
                raise HTTPException(409, f"{employee.name} is already on that shift")
            if _conflicts(schedule.week_start, a.shift, shift, 0):
                raise HTTPException(409, f"{employee.name} has an overlapping shift")
    db.add(
        Assignment(
            schedule_id=schedule_id,
            shift_id=body.shift_id,
            employee_id=body.employee_id,
            fills_level_id=body.fills_level_id,
            manual=True,
        )
    )
    db.commit()
    return _detail(db, _load_schedule(db, schedule_id))


@router.delete("/{schedule_id}/assignments/{assignment_id}", response_model=schemas.ScheduleDetail)
def remove_assignment(
    schedule_id: int,
    assignment_id: int,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    a = db.get(Assignment, assignment_id)
    if a is None or a.schedule_id != schedule_id:
        raise HTTPException(404, "Assignment not found")
    db.delete(a)
    db.commit()
    return _detail(db, _load_schedule(db, schedule_id))


@router.post("/{schedule_id}/publish", response_model=schemas.ScheduleDetail)
def publish(schedule_id: int, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    schedule = _load_schedule(db, schedule_id)
    schedule.status = "published"
    db.commit()
    return _detail(db, schedule)


@router.get("/{schedule_id}/suggestions", response_model=list[schemas.SuggestionOut])
def suggestions(
    schedule_id: int,
    shift_id: int,
    level_id: int,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    schedule = _load_schedule(db, schedule_id)
    shift = db.get(Shift, shift_id)
    level = db.get(Level, level_id)
    if shift is None or level is None:
        raise HTTPException(404, "Shift or level not found")
    return suggest_for_slot(db, schedule, shift, level)
