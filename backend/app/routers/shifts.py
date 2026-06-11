from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import schemas
from ..auth import current_user, require_manager
from ..db import get_db
from ..models import Shift, ShiftRequirement, User

router = APIRouter(prefix="/api/shifts", tags=["shifts"])


def _week_shifts(db: Session, week_start: date) -> list[Shift]:
    return list(
        db.scalars(
            select(Shift)
            .options(selectinload(Shift.requirements).selectinload(ShiftRequirement.level))
            .where(Shift.date >= week_start, Shift.date < week_start + timedelta(days=7))
            .order_by(Shift.date, Shift.start_min)
        )
    )


@router.get("", response_model=list[schemas.ShiftOut])
def list_shifts(week_start: date, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return _week_shifts(db, week_start)


@router.post("", response_model=schemas.ShiftOut)
def create_shift(
    body: schemas.ShiftIn, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    if not (0 <= body.start_min < body.end_min <= 1440):
        raise HTTPException(422, "Invalid shift times")
    shift = Shift(date=body.date, start_min=body.start_min, end_min=body.end_min, name=body.name)
    for r in body.requirements:
        shift.requirements.append(ShiftRequirement(level_id=r.level_id, count=r.count))
    db.add(shift)
    db.commit()
    db.refresh(shift)
    return shift


@router.put("/{shift_id}", response_model=schemas.ShiftOut)
def update_shift(
    shift_id: int,
    body: schemas.ShiftIn,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise HTTPException(404, "Shift not found")
    shift.date, shift.start_min, shift.end_min, shift.name = (
        body.date,
        body.start_min,
        body.end_min,
        body.name,
    )
    shift.requirements.clear()
    db.flush()
    for r in body.requirements:
        shift.requirements.append(ShiftRequirement(level_id=r.level_id, count=r.count))
    db.commit()
    db.refresh(shift)
    return shift


@router.delete("/{shift_id}")
def delete_shift(shift_id: int, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise HTTPException(404, "Shift not found")
    db.delete(shift)
    db.commit()
    return {"ok": True}


class CopyWeekIn(BaseModel):
    from_week: date
    to_week: date


@router.post("/copy-week", response_model=list[schemas.ShiftOut])
def copy_week(body: CopyWeekIn, _: User = Depends(require_manager), db: Session = Depends(get_db)):
    """Copy a week's shift pattern (times + requirements, not assignments)
    onto another week. Skips days that already have shifts."""
    src = _week_shifts(db, body.from_week)
    existing_dates = {s.date for s in _week_shifts(db, body.to_week)}
    delta = body.to_week - body.from_week
    for s in src:
        new_date = s.date + delta
        if new_date in existing_dates:
            continue
        copy = Shift(date=new_date, start_min=s.start_min, end_min=s.end_min, name=s.name)
        for r in s.requirements:
            copy.requirements.append(ShiftRequirement(level_id=r.level_id, count=r.count))
        db.add(copy)
    db.commit()
    return _week_shifts(db, body.to_week)
