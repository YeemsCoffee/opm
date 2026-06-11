from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models import (
    Assignment,
    Level,
    Schedule,
    WorkSession,
)
from app.services.solver import schedule_warnings, solve_schedule
from app.services.suggestions import suggest_for_slot
from tests.test_solver import MONDAY, _employee, _shift


def test_daily_cap_blocks_double_shift(db):
    a = _employee(db, "Doubler", "K1")
    # two 6h shifts same day, no overlap: 12h total > 8h cap
    s1 = _shift(db, MONDAY, 6, 12, [("K1", 1)])
    s2 = _shift(db, MONDAY, 12, 18, [("K1", 1)])
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()

    result = solve_schedule(db, schedule)
    mine = [s for e, s, _ in result.assignments if e == a.id]
    assert len(mine) == 1
    assert len(result.unfilled) == 1
    assert {s1.id, s2.id} >= set(mine)


def test_consecutive_days_rolling_across_weeks(db):
    a = _employee(db, "Marathon", "K1")
    level = db.scalar(select(Level).where(Level.name == "K1"))
    # timesheet history: worked the 6 days before week start (Tue..Sun)
    for i in range(1, 7):
        d = MONDAY - timedelta(days=i)
        db.add(
            WorkSession(
                employee_id=a.id,
                level_id=level.id,
                clock_in=datetime(d.year, d.month, d.day, 9),
                clock_out=datetime(d.year, d.month, d.day, 15),
            )
        )
    shifts = [_shift(db, MONDAY + timedelta(days=i), 9, 15, [("K1", 1)]) for i in range(7)]
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()

    result = solve_schedule(db, schedule)
    worked = sorted(
        next(s for s in shifts if s.id == sid).date for e, sid, _ in result.assignments
    )
    # Monday would be the 7th consecutive day -> must be skipped
    assert MONDAY not in worked
    # and no 7-day run inside the week either
    assert len(worked) <= 6


def test_warnings_flag_manual_overrides(db):
    a = _employee(db, "Pushed", "K1", max_week_minutes=2400)
    level = db.scalar(select(Level).where(Level.name == "K1"))
    # 6 days of history right before the week
    for i in range(1, 7):
        d = MONDAY - timedelta(days=i)
        db.add(
            WorkSession(
                employee_id=a.id,
                level_id=level.id,
                clock_in=datetime(d.year, d.month, d.day, 9),
                clock_out=datetime(d.year, d.month, d.day, 15),
            )
        )
    # manager manually books a 10h Monday: overtime_day + 7th consecutive day
    s = _shift(db, MONDAY, 7, 17, [("K1", 1)])
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()
    db.add(
        Assignment(
            schedule_id=schedule.id,
            shift_id=s.id,
            employee_id=a.id,
            fills_level_id=level.id,
            manual=True,
        )
    )
    db.commit()
    db.refresh(schedule)

    warnings = schedule_warnings(db, schedule, [s])
    kinds = {w["kind"] for w in warnings}
    assert "overtime_day" in kinds
    assert "consecutive_days" in kinds
    assert "overtime_week" not in kinds  # 10h < 40h cap


def test_suggestions_flag_seventh_day_and_daily_overtime(db):
    a = _employee(db, "Tired", "K1")
    level = db.scalar(select(Level).where(Level.name == "K1"))
    for i in range(1, 7):
        d = MONDAY - timedelta(days=i)
        db.add(
            WorkSession(
                employee_id=a.id,
                level_id=level.id,
                clock_in=datetime(d.year, d.month, d.day, 9),
                clock_out=datetime(d.year, d.month, d.day, 15),
            )
        )
    s = _shift(db, MONDAY, 9, 15, [("K1", 2)])
    schedule = Schedule(week_start=MONDAY)
    db.add(schedule)
    db.commit()

    suggestions = suggest_for_slot(db, schedule, s, level)
    tired = next(c for c in suggestions if c["employee_name"] == "Tired")
    assert "consecutive day #7" in tired["reason"]
    assert tired["softness"] == 2


def test_pattern_materializes_into_future_weeks(client, manager_headers):
    levels = {l["name"]: l for l in client.get("/api/levels", headers=manager_headers).json()}
    k1 = levels["K1"]
    client.post(
        "/api/employees", json={"name": "P One", "level_id": k1["id"]}, headers=manager_headers
    )
    # a reusable block, placed on Monday and Wednesday
    block = client.post(
        "/api/blocks",
        json={"name": "Open", "start_min": 390, "end_min": 870},
        headers=manager_headers,
    ).json()
    assert client.post(
        "/api/blocks", json={"name": "Open", "start_min": 0, "end_min": 60}, headers=manager_headers
    ).status_code == 409
    for wd in (0, 2):
        r = client.post(
            "/api/templates",
            json={
                "weekday": wd,
                "block_id": block["id"],
                "requirements": [{"level_id": k1["id"], "count": 1}],
            },
            headers=manager_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["block"]["name"] == "Open"
    # block in use cannot be deleted
    assert client.delete(f"/api/blocks/{block['id']}", headers=manager_headers).status_code == 409

    detail = client.post(
        "/api/schedules/generate?week_start=2026-06-15", headers=manager_headers
    ).json()
    dates = sorted(s["date"] for s in detail["shifts"])
    assert dates == ["2026-06-15", "2026-06-17"]

    # a tweaked week keeps its edits on re-generate (no duplicates)
    shift_id = detail["shifts"][0]["id"]
    client.delete(f"/api/shifts/{shift_id}", headers=manager_headers)
    detail2 = client.post(
        "/api/schedules/generate?week_start=2026-06-15", headers=manager_headers
    ).json()
    assert sorted(s["date"] for s in detail2["shifts"]) == ["2026-06-17"]

    # next week materializes fresh from the pattern
    detail3 = client.post(
        "/api/schedules/generate?week_start=2026-06-22", headers=manager_headers
    ).json()
    assert sorted(s["date"] for s in detail3["shifts"]) == ["2026-06-22", "2026-06-24"]


def test_skills_crud_and_assignment(client, manager_headers):
    levels = {l["name"]: l for l in client.get("/api/levels", headers=manager_headers).json()}
    dialing = client.post("/api/skills", json={"name": "Dialing"}, headers=manager_headers).json()
    steaming = client.post("/api/skills", json={"name": "Steaming"}, headers=manager_headers).json()
    assert client.post("/api/skills", json={"name": "Dialing"}, headers=manager_headers).status_code == 409

    emp = client.post(
        "/api/employees",
        json={"name": "Skilled", "level_id": levels["K1"]["id"], "skill_ids": [dialing["id"]]},
        headers=manager_headers,
    ).json()
    assert [s["name"] for s in emp["skills"]] == ["Dialing"]

    emp = client.patch(
        f"/api/employees/{emp['id']}",
        json={"skill_ids": [dialing["id"], steaming["id"]]},
        headers=manager_headers,
    ).json()
    assert sorted(s["name"] for s in emp["skills"]) == ["Dialing", "Steaming"]

    client.delete(f"/api/skills/{steaming['id']}", headers=manager_headers)
    emps = client.get("/api/employees", headers=manager_headers).json()
    skilled = next(e for e in emps if e["name"] == "Skilled")
    assert [s["name"] for s in skilled["skills"]] == ["Dialing"]
