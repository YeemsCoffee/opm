from datetime import date

from sqlalchemy import select

from app.models import Employee, EmployeeLevel, Level, Location
from app.services.team_import import import_team_csv

HEADER = (
    "First Name,Last Name,Email,Phone,Birthday,Location,Permission Level,"
    "PIN for Time Clock,Payroll ID,Hire Date,Wage Rate,Wage Type,Role Name\n"
)

# Mirrors the real export: Allen works two locations (named row + a blank-name
# continuation row for the second), and Hoon's payroll id only appears on his
# continuation row — both patterns seen in the real file.
TEAM_CSV = HEADER + (
    "Allen,Tran,\"\",6572625434,10/14/2002,Yeems Gardena,Employee,'533567',\"\",\"\",$22.00,Hourly,B2\n"
    "\"\",\"\",\"\",\"\",\"\",Yeems Warehouse,Employee,'533567',\"\",01/29/2026,$23.00,Hourly,Shift Lead\n"
    "Hoon,Jung,jung@example.com,2136637470,06/29/1995,Yeems Gardena,General Manager,'111111',\"\",\"\",n/a,na/,Manager\n"
    "\"\",\"\",\"\",\"\",\"\",Yeems Coffee,General Manager,'111111',pid-hoon,\"\",n/a,na/,\"\"\n"
    "Claire,Yoon,claire@example.com,5629229450,10/27/1997,Yeems Coffee,Employee,'850328',pid-claire,\"\",$21.00,Hourly,Barista 1\n"
)


def test_rejects_wrong_format(db):
    try:
        import_team_csv(db, b"Name,Role\nfoo,bar\n")
        assert False, "should have raised"
    except ValueError as exc:
        assert "team roster" in str(exc)


def test_creates_employees_and_locations(db):
    result = import_team_csv(db, TEAM_CSV.encode())
    assert result["created"] == 3
    assert result["updated"] == 0

    gardena = db.scalar(select(Location).where(Location.name == "Yeems Gardena"))
    coffee = db.scalar(select(Location).where(Location.name == "Yeems Coffee"))
    warehouse = db.scalar(select(Location).where(Location.name == "Yeems Warehouse"))
    assert gardena and coffee and warehouse

    allen = db.scalar(select(Employee).where(Employee.name == "Allen Tran"))
    assert allen.location_id == gardena.id  # first-listed location only
    assert allen.level_on(date.today()).name == "B2"  # from the named row, not the continuation

    hoon = db.scalar(select(Employee).where(Employee.name == "Hoon Jung"))
    assert hoon.location_id == gardena.id
    assert hoon.level_on(date.today()).name == "Manager"
    assert hoon.payroll_id == "pid-hoon"  # filled in from the continuation row

    claire = db.scalar(select(Employee).where(Employee.name == "Claire Yoon"))
    assert claire.location_id == coffee.id
    assert claire.level_on(date.today()).name == "Barista 1"

    assert result["additional_locations_skipped"] == 2  # Allen's Warehouse row, Hoon's Coffee row
    # B2/Manager are seeded defaults already in the system; only Barista 1 is new
    assert result["new_levels"] == ["Barista 1"]


def test_never_overwrites_an_established_level(db):
    """A real discrepancy exists: this export can list a stale role that
    contradicts an employee's actual worked level. The importer must not
    let a roster file silently overwrite level history from timesheets."""
    level = db.scalar(select(Level).where(Level.name == "Shift Lead"))
    emp = Employee(name="Allen Tran")
    db.add(emp)
    db.flush()
    db.add(EmployeeLevel(employee_id=emp.id, level_id=level.id, effective_from=date(2000, 1, 1)))
    db.commit()

    result = import_team_csv(db, TEAM_CSV.encode())
    db.refresh(emp)
    assert emp.level_on(date.today()).name == "Shift Lead"  # untouched, not overwritten to B2
    assert result["levels_kept_unchanged"] == 1
    assert "B2" not in result["new_levels"]  # never even needed to create it


def test_rerun_is_idempotent_on_location_and_updates_existing(db):
    import_team_csv(db, TEAM_CSV.encode())
    result = import_team_csv(db, TEAM_CSV.encode())
    assert result["created"] == 0
    assert result["updated"] == 3
    assert db.scalars(select(Location)).all().__len__() == 3  # no duplicate locations
