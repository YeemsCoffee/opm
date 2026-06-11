from datetime import date

from sqlalchemy import select

from app.models import (
    Assignment,
    Availability,
    Employee,
    EmployeeLevel,
    Level,
    Schedule,
    Shift,
    ShiftRequirement,
    TimeOff,
)
from app.services.solver import apply_solution, solve_schedule
from app.services.suggestions import suggest_for_slot

MONDAY = date(2026, 6, 15)


def _employee(db, name, level_name, **kw):
    level = db.scalar(select(Level).where(Level.name == level_name))
    e = Employee(name=name, **kw)
    db.add(e)
    db.flush()
    db.add(EmployeeLevel(employee_id=e.id, level_id=level.id, effective_from=date(2026, 1, 1)))
    return e


def _shift(db, d, start_h, end_h, reqs):
    s = Shift(date=d, start_min=start_h * 60, end_min=end_h * 60)
    for level_name, count in reqs:
        level = db.scalar(select(Level).where(Level.name == level_name))
        s.requirements.append(ShiftRequirement(level_id=level.id, count=count))
    db.add(s)
    return s


def test_fills_respecting_levels_and_availability(db):
    k1a = _employee(db, "K1 Alice", "K1")
    k1b = _employee(db, "K1 Bob", "K1")
    b2 = _employee(db, "B2 Cara", "B2")
    # Bob is only available Tuesdays
    db.add(Availability(employee_id=k1b.id, weekday=1, start_min=6 * 60, end_min=18 * 60))
    k1b.availability_confirmed = True

    mon = _shift(db, MONDAY, 7, 13, [("K1", 2), ("B2", 1)])
    tue = _shift(db, MONDAY.replace(day=16), 7, 13, [("K1", 1)])
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()

    result = solve_schedule(db, schedule)
    assigned = {(e, s) for e, s, _ in result.assignments}
    assert (k1a.id, mon.id) in assigned          # Alice covers Monday K1
    assert (b2.id, mon.id) in assigned           # Cara covers Monday B2
    assert (k1b.id, mon.id) not in assigned      # Bob unavailable Monday
    assert (mon.id, tue.id) != ()                # Tuesday K1 goes to Bob or Alice
    # Monday K1 slot short by one: only Alice could take it
    assert any(u["shift_id"] == mon.id and u["level_name"] == "K1" and u["missing"] == 1
               for u in result.unfilled)


def test_no_overlap_and_time_off(db):
    a = _employee(db, "Solo", "K1")
    db.add(TimeOff(employee_id=a.id, start_date=MONDAY, end_date=MONDAY, reason="vacation"))
    s1 = _shift(db, MONDAY, 7, 13, [("K1", 1)])             # time off -> unfilled
    s2 = _shift(db, MONDAY.replace(day=16), 7, 13, [("K1", 1)])
    s3 = _shift(db, MONDAY.replace(day=16), 12, 18, [("K1", 1)])  # overlaps s2
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()

    result = solve_schedule(db, schedule)
    assigned_shifts = [s for e, s, _ in result.assignments if e == a.id]
    assert s1.id not in assigned_shifts
    assert len(assigned_shifts) == 1  # s2 XOR s3, never both
    assert len(result.unfilled) == 2


def test_manual_assignments_kept_and_counted(db):
    a = _employee(db, "Anna", "K1")
    b = _employee(db, "Beth", "K1")
    s = _shift(db, MONDAY, 7, 13, [("K1", 1)])
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()
    level = db.scalar(select(Level).where(Level.name == "K1"))
    db.add(
        Assignment(
            schedule_id=schedule.id,
            shift_id=s.id,
            employee_id=b.id,
            fills_level_id=level.id,
            manual=True,
        )
    )
    db.commit()
    db.refresh(schedule)

    result = solve_schedule(db, schedule)
    # slot already covered manually by Beth; solver must not add Anna
    assert result.assignments == []
    assert result.unfilled == []
    apply_solution(db, schedule, result)
    db.refresh(schedule)
    assert [x.employee_id for x in schedule.assignments] == [b.id]


def test_higher_rated_employee_gets_the_shift(db):
    import hashlib
    from datetime import datetime, timedelta

    from app.models import Ticket, WorkSession

    good = _employee(db, "Good", "K1")
    bad = _employee(db, "Bad", "K1")
    level = db.scalar(select(Level).where(Level.name == "K1"))
    # history: alternating solo days, good=fast tickets, bad=slow tickets
    for d in range(20):
        day = MONDAY - timedelta(days=30) + timedelta(days=d)
        who, seconds = (good, 200) if d % 2 == 0 else (bad, 400)
        db.add(
            WorkSession(
                employee_id=who.id,
                level_id=level.id,
                clock_in=datetime(day.year, day.month, day.day, 9),
                clock_out=datetime(day.year, day.month, day.day, 12),
            )
        )
        for h in (9, 10, 11):
            db.add(
                Ticket(
                    created_at=datetime(day.year, day.month, day.day, h, 5),
                    completion_seconds=seconds,
                    row_hash=hashlib.sha256(f"{day}{h}".encode()).hexdigest(),
                )
            )
    s = _shift(db, MONDAY, 9, 12, [("K1", 1)])
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()

    result = solve_schedule(db, schedule)
    assert [(e, sh) for e, sh, _ in result.assignments] == [(good.id, s.id)]


def test_suggestions_for_unfilled_slot(db):
    lead = _employee(db, "Lead", "Shift Lead")
    k1_busy = _employee(db, "Busy", "K1", max_week_minutes=300)
    k1_off_hours = _employee(db, "OffHours", "K1")
    db.add(Availability(employee_id=k1_off_hours.id, weekday=5, start_min=0, end_min=1440))
    t2 = _employee(db, "Lower", "T2")  # lower rank than K1 -> never suggested

    s = _shift(db, MONDAY, 7, 13, [("K1", 2)])
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()
    level = db.scalar(select(Level).where(Level.name == "K1"))

    suggestions = suggest_for_slot(db, schedule, s, level)
    names = {c["employee_name"]: c for c in suggestions}
    assert names["Lead"]["softness"] == 0          # higher level covering down
    assert names["Busy"]["softness"] == 1          # over hours cap
    assert names["OffHours"]["softness"] == 2      # outside availability
    assert "Lower" not in names
    assert suggestions == sorted(suggestions, key=lambda c: c["softness"])
