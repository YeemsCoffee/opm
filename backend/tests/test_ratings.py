import hashlib
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models import Employee, EmployeeLevel, Level, Ticket, WorkSession
from app.services.ratings import adherence_what_if, compute_ratings


def _mk_employee(db, name, level_name="K1"):
    level = db.scalar(select(Level).where(Level.name == level_name))
    e = Employee(name=name)
    db.add(e)
    db.flush()
    db.add(EmployeeLevel(employee_id=e.id, level_id=level.id, effective_from=date(2026, 1, 1)))
    db.commit()
    return e


def _mk_ticket(db, created, seconds):
    db.add(
        Ticket(
            created_at=created,
            completion_seconds=seconds,
            row_hash=hashlib.sha256(f"{created}{seconds}".encode()).hexdigest()[:32]
            + str(created.timestamp()),
        )
    )


def test_plus_minus_separates_fast_and_slow_crews(db):
    fast = _mk_employee(db, "Fast Worker")
    slow = _mk_employee(db, "Slow Worker")
    level = db.scalar(select(Level).where(Level.name == "K1"))

    # 10 days: fast works mornings (tickets on time), slow works afternoons
    # (tickets late). Same hours every day so the hour baseline averages the two.
    for d in range(10):
        day = date(2026, 6, 1) + timedelta(days=d)
        db.add(
            WorkSession(
                employee_id=fast.id,
                level_id=level.id,
                clock_in=datetime.combine(day, datetime.min.time()).replace(hour=8),
                clock_out=datetime.combine(day, datetime.min.time()).replace(hour=12),
            )
        )
        db.add(
            WorkSession(
                employee_id=slow.id,
                level_id=level.id,
                clock_in=datetime.combine(day, datetime.min.time()).replace(hour=12),
                clock_out=datetime.combine(day, datetime.min.time()).replace(hour=16),
            )
        )
        for h in (9, 10, 11):
            for m in (0, 20, 40):
                _mk_ticket(db, datetime(day.year, day.month, day.day, h, m), 200)  # on time
        for h in (13, 14, 15):
            for m in (0, 20, 40):
                _mk_ticket(db, datetime(day.year, day.month, day.day, h, m), 400)  # late
    db.commit()

    ratings = compute_ratings(db, date(2026, 6, 1), date(2026, 6, 10))
    by_name = {r["employee_name"]: r for r in ratings}
    assert by_name["Fast Worker"]["on_floor_adherence"] == 1.0
    assert by_name["Slow Worker"]["on_floor_adherence"] == 0.0
    # different hours -> baseline is per-hour, so residual vs own hours is 0
    assert by_name["Fast Worker"]["raw_plus_minus"] == 0.0

    # now make them share hours: baseline blends, +/- separates
    for d in range(10):
        day = date(2026, 7, 1) + timedelta(days=d)
        onfloor = fast if d % 2 == 0 else slow
        db.add(
            WorkSession(
                employee_id=onfloor.id,
                level_id=level.id,
                clock_in=datetime.combine(day, datetime.min.time()).replace(hour=9),
                clock_out=datetime.combine(day, datetime.min.time()).replace(hour=12),
            )
        )
        seconds = 200 if onfloor is fast else 400
        for h in (9, 10, 11):
            for m in (0, 20, 40):
                _mk_ticket(db, datetime(day.year, day.month, day.day, h, m), seconds)
    db.commit()

    ratings = compute_ratings(db, date(2026, 7, 1), date(2026, 7, 10))
    by_name = {r["employee_name"]: r for r in ratings}
    assert by_name["Fast Worker"]["raw_plus_minus"] > 40
    assert by_name["Slow Worker"]["raw_plus_minus"] < -40
    # shrinkage pulls small samples toward 0 but keeps the sign
    assert 0 < by_name["Fast Worker"]["plus_minus"] < by_name["Fast Worker"]["raw_plus_minus"]


def test_breaks_and_training_are_excluded(db):
    emp = _mk_employee(db, "Worker")
    trainee = _mk_employee(db, "Trainee", level_name="Training")
    k1 = db.scalar(select(Level).where(Level.name == "K1"))
    training = db.scalar(select(Level).where(Level.name == "Training"))

    day = date(2026, 6, 1)
    db.add(
        WorkSession(
            employee_id=emp.id,
            level_id=k1.id,
            clock_in=datetime(2026, 6, 1, 8, 0),
            clock_out=datetime(2026, 6, 1, 12, 0),
        )
    )
    # trainee session covers the same window but must not be attributed
    db.add(
        WorkSession(
            employee_id=trainee.id,
            level_id=training.id,
            clock_in=datetime(2026, 6, 1, 8, 0),
            clock_out=datetime(2026, 6, 1, 12, 0),
        )
    )
    db.flush()
    ws = db.scalar(select(WorkSession).where(WorkSession.employee_id == emp.id))
    from app.models import BreakInterval

    ws.breaks.append(
        BreakInterval(start=datetime(2026, 6, 1, 10, 0), end=datetime(2026, 6, 1, 10, 30))
    )
    _mk_ticket(db, datetime(2026, 6, 1, 9, 0), 200)
    _mk_ticket(db, datetime(2026, 6, 1, 10, 15), 200)  # during break -> not attributed
    db.commit()

    ratings = compute_ratings(db, day, day)
    by_name = {r["employee_name"]: r for r in ratings}
    assert by_name["Worker"]["tickets"] == 1
    assert "Trainee" not in by_name


def test_what_if(db):
    _mk_ticket(db, datetime(2026, 6, 1, 9, 0), 250)
    _mk_ticket(db, datetime(2026, 6, 1, 10, 0), 350)
    db.commit()
    result = adherence_what_if(db, 300, date(2026, 6, 1), date(2026, 6, 1))
    assert result["tickets"] == 2 and result["adherence"] == 0.5
    result = adherence_what_if(db, 400, date(2026, 6, 1), date(2026, 6, 1))
    assert result["adherence"] == 1.0
