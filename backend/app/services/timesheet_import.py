"""Importer for Square Team timesheet CSV exports.

The export is block-structured: a header row per employee block, shift rows
(name + clock in/out), continuation rows carrying extra breaks for the
previous shift, "Totals for X" rows, and no-show rows (clock dates but no
times, with a reason).
"""

import csv
import io
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BreakInterval, Employee, EmployeeLevel, Level, NoShow, WorkSession

COL_NAME = 0
COL_IN_DATE = 1
COL_IN_TIME = 2
COL_OUT_DATE = 3
COL_OUT_TIME = 4
COL_BREAK_START = 5
COL_BREAK_END = 6
COL_BREAK_TYPE = 8
COL_PAYROLL_ID = 9
COL_ROLE = 10
COL_NOSHOW_REASON = 19

_SKIP_PREFIXES = ("Totals", "Name", "Payroll Period", "-")


def _parse_dt(day: str, t: str) -> datetime:
    # "June 3 2026" + "6:31am"
    return datetime.strptime(f"{day} {t.upper()}", "%B %d %Y %I:%M%p")


def _get_level(db: Session, cache: dict, name: str) -> Level:
    if name in cache:
        return cache[name]
    level = db.scalar(select(Level).where(Level.name == name))
    if level is None:
        level = Level(name=name, rank=0, counts_for_rating=(name != "Training"))
        db.add(level)
        db.flush()
    cache[name] = level
    return level


def _get_employee(db: Session, cache: dict, name: str, payroll_id: str) -> Employee:
    if name in cache:
        emp = cache[name]
    else:
        emp = db.scalar(select(Employee).where(Employee.name == name))
        if emp is None and payroll_id:
            emp = db.scalar(select(Employee).where(Employee.payroll_id == payroll_id))
        if emp is None:
            emp = Employee(name=name)
            db.add(emp)
            db.flush()
        cache[name] = emp
    if payroll_id and not emp.payroll_id:
        emp.payroll_id = payroll_id
    return emp


def import_timesheet_csv(db: Session, data: bytes) -> dict:
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not any(r and r[COL_NAME].strip() == "Name" for r in rows):
        raise ValueError("Not a timesheet export: no employee header rows found")

    level_cache: dict[str, Level] = {}
    emp_cache: dict[str, Employee] = {}
    sessions_created = sessions_skipped = noshows = 0
    current: WorkSession | None = None
    current_day: str = ""

    for row in rows:
        if len(row) <= COL_NOSHOW_REASON:
            continue
        name = row[COL_NAME].strip()
        if name and not name.startswith(_SKIP_PREFIXES):
            current = None
            role = row[COL_ROLE].strip()
            if not role:
                continue  # location title row or similar
            emp = _get_employee(db, emp_cache, name, row[COL_PAYROLL_ID].strip())
            if row[COL_IN_TIME].strip() and row[COL_OUT_TIME].strip():
                level = _get_level(db, level_cache, role)
                clock_in = _parse_dt(row[COL_IN_DATE], row[COL_IN_TIME])
                clock_out = _parse_dt(row[COL_OUT_DATE], row[COL_OUT_TIME])
                dup = db.scalar(
                    select(WorkSession).where(
                        WorkSession.employee_id == emp.id, WorkSession.clock_in == clock_in
                    )
                )
                if dup is not None:
                    sessions_skipped += 1
                    current = None
                    continue
                current = WorkSession(
                    employee_id=emp.id, level_id=level.id, clock_in=clock_in, clock_out=clock_out
                )
                current_day = row[COL_IN_DATE]
                db.add(current)
                sessions_created += 1
                _add_break(current, current_day, row)
            elif row[COL_NOSHOW_REASON].strip():
                ns_date = datetime.strptime(row[COL_IN_DATE], "%B %d %Y").date()
                reason = row[COL_NOSHOW_REASON].strip()
                dup = db.scalar(
                    select(NoShow).where(
                        NoShow.employee_id == emp.id,
                        NoShow.date == ns_date,
                        NoShow.reason == reason,
                    )
                )
                if dup is None:
                    db.add(NoShow(employee_id=emp.id, date=ns_date, reason=reason))
                    noshows += 1
        elif not name and current is not None:
            _add_break(current, current_day, row)

    db.flush()
    levels_updated = _update_level_history(db, emp_cache.values())
    db.commit()
    return {
        "created": sessions_created,
        "skipped": sessions_skipped,
        "no_shows": noshows,
        "levels_updated": levels_updated,
    }


def _add_break(session: WorkSession, day: str, row: list[str]) -> None:
    if row[COL_BREAK_START].strip() and row[COL_BREAK_END].strip():
        start = _parse_dt(day, row[COL_BREAK_START])
        end = _parse_dt(day, row[COL_BREAK_END])
        paid = "Unpaid" not in (row[COL_BREAK_TYPE] or "")
        session.breaks.append(BreakInterval(start=start, end=end, paid=paid))


def _update_level_history(db: Session, employees) -> int:
    """Set each employee's level to the one worked in their latest
    non-training session, effective from the first day it appears."""
    updated = 0
    for emp in employees:
        sessions = db.scalars(
            select(WorkSession)
            .join(Level, WorkSession.level_id == Level.id)
            .where(WorkSession.employee_id == emp.id, Level.counts_for_rating.is_(True))
            .order_by(WorkSession.clock_in)
        ).all()
        if not sessions:
            continue
        latest_level_id = sessions[-1].level_id
        first_at_level = sessions[-1].clock_in.date()
        for s in reversed(sessions):
            if s.level_id != latest_level_id:
                break
            first_at_level = s.clock_in.date()
        current = emp.level_on(date.max)
        if current is None or current.id != latest_level_id:
            db.add(
                EmployeeLevel(
                    employee_id=emp.id, level_id=latest_level_id, effective_from=first_at_level
                )
            )
            db.flush()
            db.refresh(emp)
            updated += 1
    return updated
