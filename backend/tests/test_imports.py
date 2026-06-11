from datetime import date, datetime

from sqlalchemy import select

from app.models import BreakInterval, Employee, NoShow, Ticket, WorkSession
from app.services.kitchen_import import import_kitchen_csv
from app.services.timesheet_import import import_timesheet_csv

KITCHEN_CSV = """Device Name,Ticket Name,Order Source,Number of Items,Items in Ticket,Completion Time (seconds),Time Created,Time Completed,Time Due,Time Recalled
Main bar,Alice,Point of Sale,1,1x Latte,250,6/2/2026 9:05,6/2/2026 9:09,,
Main bar,123456,Square Online,2,2x Mocha,400,6/2/2026 9:10,6/2/2026 9:17,6/2/2026 9:20,
Main bar,Bob,Point of Sale,1,1x Drip,120,6/2/2026 14:00,6/2/2026 14:02,,
Main bar,Carol,Point of Sale,1,1x Tea,500,6/2/2026 14:05,6/2/2026 14:13,,2026-06-02T21:14:00.000Z
"""

TIMESHEET_CSV = '''Test Cafe,"","","","","","","","","","","","","","","","","","","","",""
Payroll Period,06/01/2026 To 06/09/2026,"","","","","","","","","","","","","","","","","","","",""
"","","","","","","","","","","","","","","","","","","","","",""
Name,Clock in date,Clock in time,Clock out date,Clock out time,Break start,Break end,Break length,Break type,Payroll ID,Role,Scheduled hours,Actual vs. scheduled,Total paid hours,Regular hours,Unpaid breaks,Estimated wages,Cash tips,Credit tips,No show reason,Employee note,Manager note
Ann Worker,June 2 2026,6:30am,June 2 2026,2:30pm,8:30am,8:40am,10 min,10 min - Paid,"pid-1",K1,8.00,0.00,8.00,8.00,0.50,$160.00,$0.00,$10.00,"","",""
"","","","","",10:30am,11:00am,30 min,30 min - Unpaid,"","","","","","","","","","","","",""
Ann Worker,June 3 2026,"",June 3 2026,"","","","","","pid-1",K1,5.50,-5.50,0.00,0.00,0.00,$0.00,$0.00,$0.00,Sick,"",""
Totals for Ann Worker,"","","","","","","","","","",13.50,-5.50,8.00,8.00,0.50,$160.00,$0.00,$10.00,"","",""
-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-
"","","","","","","","","","","","","","","","","","","","","",""
Name,Clock in date,Clock in time,Clock out date,Clock out time,Break start,Break end,Break length,Break type,Payroll ID,Role,Scheduled hours,Actual vs. scheduled,Total paid hours,Regular hours,Unpaid breaks,Estimated wages,Cash tips,Credit tips,No show reason,Employee note,Manager note
Ben Barista,June 2 2026,9:00am,June 2 2026,5:00pm,12:00pm,12:30pm,30 min,30 min - Unpaid,"",B2,8.00,0.00,8.00,8.00,0.50,$176.00,$0.00,$12.00,"","",""
Ben Barista,June 4 2026,6:00pm,June 4 2026,7:30pm,"","","","","",Training,0.00,1.50,1.50,1.50,0.00,$33.00,$0.00,$0.00,"","",meeting
Totals for Ben Barista,"","","","","","","","","","",8.00,1.50,9.50,9.50,0.50,$209.00,$0.00,$12.00,"","",""
-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-
Totals,"","","","","","","","","","",21.50,-4.00,17.50,17.50,1.00,$369.00,$0.00,$22.00,"","",""
'''


def test_kitchen_import_and_dedupe(db):
    result = import_kitchen_csv(db, KITCHEN_CSV.encode())
    assert result == {"created": 4, "skipped": 0}
    again = import_kitchen_csv(db, KITCHEN_CSV.encode())
    assert again == {"created": 0, "skipped": 4}

    tickets = db.scalars(select(Ticket).order_by(Ticket.created_at)).all()
    assert tickets[0].created_at == datetime(2026, 6, 2, 9, 5)
    assert tickets[0].completion_seconds == 250
    assert tickets[1].source == "Square Online"
    assert tickets[3].recalled is True


def test_kitchen_import_rejects_wrong_file(db):
    try:
        import_kitchen_csv(db, b"Name,Clock in date\nfoo,bar\n")
        assert False, "should have raised"
    except ValueError as exc:
        assert "kitchen report" in str(exc)


def test_timesheet_import(db):
    result = import_timesheet_csv(db, TIMESHEET_CSV.encode())
    assert result["created"] == 3
    assert result["no_shows"] == 1
    assert result["levels_updated"] == 2

    ann = db.scalar(select(Employee).where(Employee.name == "Ann Worker"))
    assert ann.payroll_id == "pid-1"
    assert ann.level_on(date(2026, 6, 9)).name == "K1"

    sessions = db.scalars(
        select(WorkSession).where(WorkSession.employee_id == ann.id)
    ).all()
    assert len(sessions) == 1
    assert sessions[0].clock_in == datetime(2026, 6, 2, 6, 30)
    assert sessions[0].clock_out == datetime(2026, 6, 2, 14, 30)
    breaks = sorted(sessions[0].breaks, key=lambda b: b.start)
    assert len(breaks) == 2
    assert breaks[0].paid is True
    assert breaks[1].paid is False
    assert breaks[1].start == datetime(2026, 6, 2, 10, 30)

    ben = db.scalar(select(Employee).where(Employee.name == "Ben Barista"))
    # latest *non-training* session decides the level
    assert ben.level_on(date(2026, 6, 9)).name == "B2"

    ns = db.scalars(select(NoShow)).all()
    assert len(ns) == 1 and ns[0].reason == "Sick" and ns[0].date == date(2026, 6, 3)

    # re-import is idempotent
    again = import_timesheet_csv(db, TIMESHEET_CSV.encode())
    assert again["created"] == 0 and again["skipped"] == 3 and again["no_shows"] == 0
    assert len(db.scalars(select(BreakInterval)).all()) == 3
