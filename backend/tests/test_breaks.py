from collections import Counter
from datetime import date

from sqlalchemy import select

from app.models import BreakConfig, BreakRule, RosterEntry
from app.services.breaks import entitlement, solve_breaks

DAY = date(2026, 7, 6)


def _entry(db, name, start_h, end_h, start_m=0, end_m=0):
    e = RosterEntry(
        date=DAY, name=name, start_min=start_h * 60 + start_m, end_min=end_h * 60 + end_m
    )
    db.add(e)
    db.flush()
    return e


def test_entitlement_rules(db):
    rules = list(db.scalars(select(BreakRule)))
    assert entitlement(120, rules) == (0, 0)      # 2h: nothing
    assert entitlement(330, rules) == (1, 0)      # 5.5h: one paid 10
    assert entitlement(480, rules) == (2, 1)      # 8h: two paid 10s + meal


def test_break_plan_respects_all_rules(db):
    cfg = db.scalar(select(BreakConfig))
    entries = [
        _entry(db, "Opener A", 6, 14, 30, 30),   # 8h -> 2 rest + meal
        _entry(db, "Opener B", 6, 14, 30, 30),   # 8h -> 2 rest + meal
        _entry(db, "Mid", 9, 14, 0, 30),         # 5.5h -> 1 rest
        _entry(db, "Short", 12, 14),             # 2h -> nothing
    ]
    db.commit()

    plan = solve_breaks(db, entries)
    by_entry = Counter(b.roster_entry_id for b in plan)
    assert by_entry[entries[0].id] == 3
    assert by_entry[entries[1].id] == 3
    assert by_entry[entries[2].id] == 1
    assert entries[3].id not in by_entry

    lookup = {e.id: e for e in entries}
    for b in plan:
        e = lookup[b.roster_entry_id]
        # within shift, away from edges
        assert b.start_min >= e.start_min + cfg.edge_pad_minutes
        assert b.end_min <= e.end_min - cfg.edge_pad_minutes
        if b.kind == "meal":
            assert not b.paid
            assert b.end_min - b.start_min == cfg.meal_minutes
            # meal starts before the 5th hour ends
            assert b.start_min <= e.start_min + cfg.meal_by_minute
        else:
            assert b.paid
            assert b.end_min - b.start_min == cfg.rest_minutes

    # one person's breaks are ordered and spaced
    for e in entries[:2]:
        mine = sorted((b for b in plan if b.roster_entry_id == e.id), key=lambda b: b.start_min)
        for first, second in zip(mine, mine[1:]):
            assert second.start_min >= first.end_min + cfg.min_gap_minutes

    # coverage: never two people on break in the same minute
    minutes = Counter()
    for b in plan:
        for m in range(b.start_min, b.end_min):
            minutes[m] += 1
    assert max(minutes.values()) <= cfg.max_concurrent


def test_breaks_api_flow(client, manager_headers):
    day = "2026-07-06"
    r = client.get(f"/api/breaks?date={day}", headers=manager_headers)
    assert r.status_code == 200
    assert r.json()["roster"] == []
    assert r.json()["homebase_configured"] is False

    # generate with no roster fails cleanly
    assert client.post(f"/api/breaks/generate?date={day}", headers=manager_headers).status_code == 422

    for name, s, e in [("Ana", 390, 870), ("Ben", 390, 870), ("Cy", 720, 1080)]:
        r = client.post(
            "/api/breaks/roster/manual",
            json={"date": day, "name": name, "start_min": s, "end_min": e, "role": "K1"},
            headers=manager_headers,
        )
        assert r.status_code == 200
    d = client.post(f"/api/breaks/generate?date={day}", headers=manager_headers).json()
    breaks = {p["name"]: p["breaks"] for p in d["roster"]}
    assert len(breaks["Ana"]) == 3 and len(breaks["Ben"]) == 3
    assert len(breaks["Cy"]) == 1  # 6h -> one rest

    # move a break, still inside the shift
    item = breaks["Ana"][0]
    r = client.patch(
        f"/api/breaks/items/{item['id']}", json={"start_min": 600}, headers=manager_headers
    )
    assert r.status_code == 200
    # move outside the shift is rejected
    assert client.patch(
        f"/api/breaks/items/{item['id']}", json={"start_min": 60}, headers=manager_headers
    ).status_code == 422

    # delete a person, their breaks go with them
    ana_id = next(p["id"] for p in d["roster"] if p["name"] == "Ana")
    d2 = client.delete(f"/api/breaks/roster/{ana_id}", headers=manager_headers).json()
    assert all(p["name"] != "Ana" for p in d2["roster"])

    # homebase import without config gives a clear 502
    r = client.post(f"/api/breaks/roster/homebase?date={day}", headers=manager_headers)
    assert r.status_code == 502
    assert "not configured" in r.json()["detail"]


def test_breaks_from_internal_schedule(client, manager_headers):
    levels = {l["name"]: l for l in client.get("/api/levels", headers=manager_headers).json()}
    k1 = levels["K1"]
    client.post("/api/employees", json={"name": "W One", "level_id": k1["id"]}, headers=manager_headers)
    client.post(
        "/api/shifts",
        json={"date": "2026-06-15", "start_min": 390, "end_min": 870,
              "requirements": [{"level_id": k1["id"], "count": 1}]},
        headers=manager_headers,
    )
    client.post("/api/schedules/generate?week_start=2026-06-15", headers=manager_headers)
    d = client.post("/api/breaks/roster/internal?date=2026-06-15", headers=manager_headers).json()
    assert [p["name"] for p in d["roster"]] == ["W One"]
    assert d["roster"][0]["source"] == "internal"
    d = client.post("/api/breaks/generate?date=2026-06-15", headers=manager_headers).json()
    assert len(d["roster"][0]["breaks"]) == 3  # 8h shift


def test_break_rules_editable(client, manager_headers):
    r = client.put(
        "/api/breaks/rules",
        json=[{"min_shift_minutes": 240, "rest_breaks": 1, "meal_breaks": 1}],
        headers=manager_headers,
    )
    assert r.status_code == 200 and len(r.json()) == 1
    assert client.put(
        "/api/breaks/rules",
        json=[{"min_shift_minutes": 240, "rest_breaks": 1, "meal_breaks": 0},
              {"min_shift_minutes": 240, "rest_breaks": 2, "meal_breaks": 0}],
        headers=manager_headers,
    ).status_code == 422
