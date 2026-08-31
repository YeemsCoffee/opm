"""Importer for Homebase's Team roster CSV export.

Columns: First Name, Last Name, Email, Phone, Birthday, Location,
Permission Level, PIN for Time Clock, Payroll ID, Hire Date, Wage Rate,
Wage Type, Role Name.

One row per (employee, location): a person working multiple locations gets
one row per location, with name blank on every row after the first — same
continuation-block pattern as the timesheet export. Only the first
(named) row's location is assigned to the employee, since an employee
currently belongs to a single location; a person's other locations are
still registered (so they exist to assign manually later) but not applied,
and counted separately in the result.

Level/role is intentionally NOT overwritten for an employee who already
has one (e.g. from a timesheet import): a real discrepancy was found where
this export's Role Name for an employee didn't match their actual worked
role, so this file is trusted for location, not as an authority that
overrides an already-established level.
"""

import csv
import io
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, EmployeeLevel, Level, Location

REQUIRED_COLUMNS = {"First Name", "Last Name", "Location", "Role Name"}


def _get_location(db: Session, cache: dict, name: str) -> Location | None:
    name = name.strip()
    if not name:
        return None
    if name in cache:
        return cache[name]
    loc = db.scalar(select(Location).where(Location.name == name))
    if loc is None:
        loc = Location(name=name)
        db.add(loc)
        db.flush()
    cache[name] = loc
    return loc


def _get_level(db: Session, cache: dict, name: str, new_levels: set[str]) -> Level | None:
    name = name.strip()
    if not name:
        return None
    if name in cache:
        return cache[name]
    level = db.scalar(select(Level).where(Level.name == name))
    if level is None:
        level = Level(name=name, rank=0, counts_for_rating=True)
        db.add(level)
        db.flush()
        new_levels.add(name)
    cache[name] = level
    return level


def import_team_csv(db: Session, data: bytes) -> dict:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
        raise ValueError(
            "Not a team roster export: missing columns "
            + ", ".join(sorted(REQUIRED_COLUMNS - set(reader.fieldnames or [])))
        )

    location_cache: dict[str, Location] = {}
    level_cache: dict[str, Level] = {}
    new_levels: set[str] = set()
    created = updated = 0
    additional_locations_skipped = 0
    levels_kept_unchanged = 0
    current_employee: Employee | None = None
    today = date.today()

    for row in reader:
        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        location_name = (row.get("Location") or "").strip()
        role_name = (row.get("Role Name") or "").strip()
        payroll_id = (row.get("Payroll ID") or "").strip()

        if first:
            name = f"{first} {last}".strip()
            emp = db.scalar(select(Employee).where(Employee.name == name))
            if emp is None and payroll_id:
                emp = db.scalar(select(Employee).where(Employee.payroll_id == payroll_id))
            if emp is None:
                emp = Employee(name=name)
                db.add(emp)
                db.flush()
                created += 1
            else:
                updated += 1
            current_employee = emp

            if payroll_id and not emp.payroll_id:
                emp.payroll_id = payroll_id

            location = _get_location(db, location_cache, location_name)
            if location is not None:
                emp.location_id = location.id

            if role_name:
                if emp.level_on(today) is not None:
                    levels_kept_unchanged += 1
                else:
                    level = _get_level(db, level_cache, role_name, new_levels)
                    db.add(
                        EmployeeLevel(
                            employee_id=emp.id, level_id=level.id, effective_from=date(2000, 1, 1)
                        )
                    )
        else:
            if current_employee is None:
                continue  # malformed: a continuation row with nothing to continue
            if payroll_id and not current_employee.payroll_id:
                current_employee.payroll_id = payroll_id
            # register the location (so it shows up to assign manually later)
            # without reassigning this employee to it
            _get_location(db, location_cache, location_name)
            additional_locations_skipped += 1

    db.commit()
    return {
        "created": created,
        "updated": updated,
        "new_levels": sorted(new_levels),
        "additional_locations_skipped": additional_locations_skipped,
        "levels_kept_unchanged": levels_kept_unchanged,
    }
