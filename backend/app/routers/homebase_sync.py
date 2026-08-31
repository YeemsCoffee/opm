from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import require_manager
from ..db import get_db
from ..models import Employee, HomebaseSyncStatus, HoursSnapshot, ShiftSwap, User
from ..services.homebase_browser import sync_once

router = APIRouter(prefix="/api/homebase-sync", tags=["homebase-sync"])


def _name_to_employee_id(db: Session) -> dict[str, int]:
    return {e.name.lower(): e.id for e in db.scalars(select(Employee))}


@router.get("/status", response_model=schemas.HomebaseStatusOut)
def get_status(_: User = Depends(require_manager), db: Session = Depends(get_db)):
    return db.scalar(select(HomebaseSyncStatus)) or HomebaseSyncStatus()


@router.post("/run", response_model=schemas.HomebaseStatusOut)
def run_sync_now(_: User = Depends(require_manager), db: Session = Depends(get_db)):
    """Runs the same scrape the scheduled script runs, synchronously. Meant
    for an on-demand refresh from the UI — the daily Task Scheduler job is
    what keeps this current without anyone clicking anything."""
    sync_once(db)
    return db.scalar(select(HomebaseSyncStatus))


@router.get("/hours", response_model=list[schemas.HoursSnapshotOut])
def get_hours(
    period_start: date | None = None,
    period_end: date | None = None,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    q = select(HoursSnapshot)
    if period_start:
        q = q.where(HoursSnapshot.period_start == period_start)
    if period_end:
        q = q.where(HoursSnapshot.period_end == period_end)
    rows = list(db.scalars(q.order_by(HoursSnapshot.period_start.desc(), HoursSnapshot.employee_name)))
    by_name = _name_to_employee_id(db)
    return [
        schemas.HoursSnapshotOut(
            employee_name=r.employee_name,
            period_start=r.period_start,
            period_end=r.period_end,
            hours=r.hours,
            synced_at=r.synced_at,
            matched_employee_id=by_name.get(r.employee_name.lower()),
        )
        for r in rows
    ]


@router.get("/swaps", response_model=list[schemas.ShiftSwapOut])
def get_swaps(
    start: date | None = None,
    end: date | None = None,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    q = select(ShiftSwap)
    if start:
        q = q.where(ShiftSwap.shift_date >= start)
    if end:
        q = q.where(ShiftSwap.shift_date <= end)
    rows = list(db.scalars(q.order_by(ShiftSwap.shift_date.desc())))
    by_name = _name_to_employee_id(db)
    return [
        schemas.ShiftSwapOut(
            id=r.id,
            shift_date=r.shift_date,
            start_min=r.start_min,
            end_min=r.end_min,
            released_by=r.released_by,
            covered_by=r.covered_by,
            role=r.role,
            status=r.status,
            synced_at=r.synced_at,
            covered_by_employee_id=by_name.get(r.covered_by.lower()),
            released_by_employee_id=by_name.get(r.released_by.lower()),
        )
        for r in rows
    ]
