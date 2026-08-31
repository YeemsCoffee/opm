from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import schemas
from ..auth import current_user, require_manager
from ..db import get_db
from ..models import (
    Assignment,
    BreakConfig,
    BreakPlanItem,
    BreakRule,
    RosterEntry,
    Schedule,
    Shift,
    User,
)
from ..services.breaks import apply_break_plan, solve_breaks
from ..services.homebase import fetch_day_roster, homebase_configured

router = APIRouter(prefix="/api/breaks", tags=["breaks"])


def _day_roster(db: Session, day: date) -> list[RosterEntry]:
    return list(
        db.scalars(
            select(RosterEntry)
            .options(selectinload(RosterEntry.breaks))
            .where(RosterEntry.date == day)
            .order_by(RosterEntry.start_min, RosterEntry.name)
            # refresh the breaks collection even when the entries are already
            # in the identity map with a stale (pre-generate) collection
            .execution_options(populate_existing=True)
        )
    )


def _day_out(db: Session, day: date) -> dict:
    return {
        "date": day,
        "roster": _day_roster(db, day),
        "homebase_configured": homebase_configured(),
    }


@router.get("", response_model=schemas.BreakDayOut)
def get_day(date: date, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return _day_out(db, date)


@router.post("/roster/manual", response_model=schemas.BreakDayOut)
def add_entry(
    body: schemas.RosterEntryIn, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    if not body.name.strip():
        raise HTTPException(422, "Name required")
    if not (0 <= body.start_min < body.end_min <= 1440):
        raise HTTPException(422, "Invalid shift times")
    db.add(RosterEntry(**{**body.model_dump(), "name": body.name.strip()}, source="manual"))
    db.commit()
    return _day_out(db, body.date)


@router.delete("/roster/{entry_id}", response_model=schemas.BreakDayOut)
def delete_entry(entry_id: int, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    entry = db.get(RosterEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "Roster entry not found")
    day = entry.date
    db.delete(entry)
    db.commit()
    return _day_out(db, day)


@router.post("/roster/internal", response_model=schemas.BreakDayOut)
def import_from_schedule(
    date: date, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    """Replace the day's roster with this app's schedule for that date."""
    week_start = date - timedelta(days=date.weekday())
    schedule = db.scalar(select(Schedule).where(Schedule.week_start == week_start))
    if schedule is None:
        raise HTTPException(404, "No schedule exists in this app for that week")
    rows = db.execute(
        select(Assignment, Shift)
        .join(Shift, Assignment.shift_id == Shift.id)
        .where(Assignment.schedule_id == schedule.id, Shift.date == date)
    ).all()
    if not rows:
        raise HTTPException(404, "The app schedule has no shifts on that date")
    for entry in _day_roster(db, date):
        db.delete(entry)
    db.flush()
    for a, s in rows:
        lvl = a.employee.level_on(date)
        db.add(
            RosterEntry(
                date=date,
                name=a.employee.name,
                role=lvl.name if lvl else "",
                start_min=s.start_min,
                end_min=s.end_min,
                source="internal",
            )
        )
    db.commit()
    return _day_out(db, date)


@router.post("/roster/homebase", response_model=schemas.BreakDayOut)
def import_from_homebase(
    date: date, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    """Replace the day's roster with the schedule Homebase has for that date."""
    try:
        fetched = fetch_day_roster(date)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    if not fetched:
        raise HTTPException(404, "Homebase returned no shifts for that date")
    for entry in _day_roster(db, date):
        db.delete(entry)
    db.flush()
    for r in fetched:
        db.add(RosterEntry(date=date, source="homebase", **r))
    db.commit()
    return _day_out(db, date)


@router.post("/generate", response_model=schemas.BreakDayOut)
def generate(date: date, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    roster = _day_roster(db, date)
    if not roster:
        raise HTTPException(422, "No roster for that date — import or add the day's shifts first")
    plan = solve_breaks(db, roster)
    apply_break_plan(db, roster, plan)
    return _day_out(db, date)


@router.patch("/items/{item_id}", response_model=schemas.BreakDayOut)
def move_break(
    item_id: int,
    body: schemas.BreakItemMove,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    item = db.get(BreakPlanItem, item_id)
    if item is None:
        raise HTTPException(404, "Break not found")
    duration = item.end_min - item.start_min
    entry = item.entry
    if body.start_min < entry.start_min or body.start_min + duration > entry.end_min:
        raise HTTPException(422, "Break must stay within the shift")
    item.start_min = body.start_min
    item.end_min = body.start_min + duration
    db.commit()
    return _day_out(db, entry.date)


# --- break settings ---

@router.get("/config", response_model=schemas.BreakConfigOut)
def get_config(_: User = Depends(require_manager), db: Session = Depends(get_db)):
    return db.scalar(select(BreakConfig))


@router.patch("/config", response_model=schemas.BreakConfigOut)
def update_config(
    body: schemas.BreakConfigIn, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    cfg = db.scalar(select(BreakConfig))
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(cfg, field, value)
    db.commit()
    return cfg


@router.get("/rules", response_model=list[schemas.BreakRuleOut])
def list_rules(_: User = Depends(require_manager), db: Session = Depends(get_db)):
    return db.scalars(select(BreakRule).order_by(BreakRule.min_shift_minutes)).all()


@router.put("/rules", response_model=list[schemas.BreakRuleOut])
def replace_rules(
    rules: list[schemas.BreakRuleIn], _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    seen = set()
    for r in rules:
        if r.min_shift_minutes in seen:
            raise HTTPException(422, "Duplicate shift-length threshold")
        seen.add(r.min_shift_minutes)
        if r.rest_breaks < 0 or r.meal_breaks < 0 or r.min_shift_minutes < 0:
            raise HTTPException(422, "Rule values must be non-negative")
    for row in db.scalars(select(BreakRule)):
        db.delete(row)
    db.flush()
    for r in rules:
        db.add(BreakRule(**r.model_dump()))
    db.commit()
    return db.scalars(select(BreakRule).order_by(BreakRule.min_shift_minutes)).all()
