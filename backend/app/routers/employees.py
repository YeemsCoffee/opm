from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import current_user, require_manager
from ..db import get_db
from ..models import Availability, Employee, EmployeeLevel, Level, TimeOff, User

router = APIRouter(prefix="/api", tags=["employees"])


def _employee_out(e: Employee) -> dict:
    lvl = e.level_on(date.today())
    return {
        "id": e.id,
        "name": e.name,
        "payroll_id": e.payroll_id,
        "active": e.active,
        "max_week_minutes": e.max_week_minutes,
        "target_week_minutes": e.target_week_minutes,
        "availability_confirmed": e.availability_confirmed,
        "level": lvl,
    }


def _can_edit(user: User, employee_id: int) -> bool:
    return user.role == "manager" or user.employee_id == employee_id


@router.get("/levels", response_model=list[schemas.LevelOut])
def list_levels(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Level).order_by(Level.rank.desc())).all()


@router.patch("/levels/{level_id}", response_model=schemas.LevelOut)
def update_level(
    level_id: int,
    body: schemas.LevelUpdate,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    level = db.get(Level, level_id)
    if level is None:
        raise HTTPException(404, "Level not found")
    if body.rank is not None:
        level.rank = body.rank
    if body.counts_for_rating is not None:
        level.counts_for_rating = body.counts_for_rating
    db.commit()
    return level


@router.get("/employees", response_model=list[schemas.EmployeeOut])
def list_employees(_: User = Depends(current_user), db: Session = Depends(get_db)):
    employees = db.scalars(select(Employee).order_by(Employee.name)).all()
    return [_employee_out(e) for e in employees]


@router.post("/employees", response_model=schemas.EmployeeOut)
def create_employee(
    body: schemas.EmployeeIn, _: User = Depends(require_manager), db: Session = Depends(get_db)
):
    if db.scalar(select(Employee).where(Employee.name == body.name)):
        raise HTTPException(409, "Employee with that name already exists")
    e = Employee(
        name=body.name,
        payroll_id=body.payroll_id,
        max_week_minutes=body.max_week_minutes,
        target_week_minutes=body.target_week_minutes,
        active=body.active,
    )
    db.add(e)
    db.flush()
    db.add(EmployeeLevel(employee_id=e.id, level_id=body.level_id, effective_from=date.today()))
    db.commit()
    db.refresh(e)
    return _employee_out(e)


@router.patch("/employees/{employee_id}", response_model=schemas.EmployeeOut)
def update_employee(
    employee_id: int,
    body: schemas.EmployeeUpdate,
    _: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    e = db.get(Employee, employee_id)
    if e is None:
        raise HTTPException(404, "Employee not found")
    for attr in ("name", "max_week_minutes", "target_week_minutes", "active"):
        val = getattr(body, attr)
        if val is not None:
            setattr(e, attr, val)
    if body.level_id is not None:
        current = e.level_on(date.today())
        if current is None or current.id != body.level_id:
            existing = db.scalar(
                select(EmployeeLevel).where(
                    EmployeeLevel.employee_id == e.id,
                    EmployeeLevel.effective_from == date.today(),
                )
            )
            if existing:
                existing.level_id = body.level_id
            else:
                db.add(
                    EmployeeLevel(
                        employee_id=e.id, level_id=body.level_id, effective_from=date.today()
                    )
                )
    db.commit()
    db.refresh(e)
    return _employee_out(e)


# --- availability ---

@router.get("/employees/{employee_id}/availability", response_model=list[schemas.AvailabilityOut])
def get_availability(
    employee_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    if not _can_edit(user, employee_id):
        raise HTTPException(403, "Not your availability")
    return db.scalars(
        select(Availability)
        .where(Availability.employee_id == employee_id)
        .order_by(Availability.weekday, Availability.start_min)
    ).all()


@router.put("/employees/{employee_id}/availability", response_model=list[schemas.AvailabilityOut])
def set_availability(
    employee_id: int,
    windows: list[schemas.AvailabilityIn],
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not _can_edit(user, employee_id):
        raise HTTPException(403, "Not your availability")
    e = db.get(Employee, employee_id)
    if e is None:
        raise HTTPException(404, "Employee not found")
    for w in windows:
        if not (0 <= w.weekday <= 6) or not (0 <= w.start_min < w.end_min <= 1440):
            raise HTTPException(422, "Invalid availability window")
    for row in db.scalars(select(Availability).where(Availability.employee_id == employee_id)):
        db.delete(row)
    for w in windows:
        db.add(Availability(employee_id=employee_id, **w.model_dump()))
    e.availability_confirmed = True
    db.commit()
    return db.scalars(
        select(Availability)
        .where(Availability.employee_id == employee_id)
        .order_by(Availability.weekday, Availability.start_min)
    ).all()


# --- time off ---

@router.get("/employees/{employee_id}/time-off", response_model=list[schemas.TimeOffOut])
def get_time_off(
    employee_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    if not _can_edit(user, employee_id):
        raise HTTPException(403, "Not your time off")
    return db.scalars(
        select(TimeOff).where(TimeOff.employee_id == employee_id).order_by(TimeOff.start_date)
    ).all()


@router.post("/employees/{employee_id}/time-off", response_model=schemas.TimeOffOut)
def add_time_off(
    employee_id: int,
    body: schemas.TimeOffIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not _can_edit(user, employee_id):
        raise HTTPException(403, "Not your time off")
    if body.end_date < body.start_date:
        raise HTTPException(422, "End date before start date")
    row = TimeOff(employee_id=employee_id, **body.model_dump())
    db.add(row)
    db.commit()
    return row


@router.delete("/employees/{employee_id}/time-off/{time_off_id}")
def delete_time_off(
    employee_id: int,
    time_off_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not _can_edit(user, employee_id):
        raise HTTPException(403, "Not your time off")
    row = db.get(TimeOff, time_off_id)
    if row is None or row.employee_id != employee_id:
        raise HTTPException(404, "Not found")
    db.delete(row)
    db.commit()
    return {"ok": True}
