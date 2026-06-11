from datetime import date

MONDAY = "2026-06-15"


def test_register_login_and_lockout(client):
    r = client.post("/api/auth/register", json={"email": "boss@x.com", "password": "pw123456"})
    assert r.status_code == 200 and r.json()["role"] == "manager"
    # second open registration refused
    r2 = client.post("/api/auth/register", json={"email": "evil@x.com", "password": "pw123456"})
    assert r2.status_code == 403
    r3 = client.post("/api/auth/login", json={"email": "boss@x.com", "password": "pw123456"})
    assert r3.status_code == 200
    assert client.post("/api/auth/login", json={"email": "boss@x.com", "password": "nope"}).status_code == 401


def test_employee_crud_and_role_limits(client, manager_headers):
    levels = client.get("/api/levels", headers=manager_headers).json()
    k1 = next(l for l in levels if l["name"] == "K1")

    r = client.post(
        "/api/employees", json={"name": "Test Person", "level_id": k1["id"]}, headers=manager_headers
    )
    assert r.status_code == 200
    emp = r.json()
    assert emp["level"]["name"] == "K1"

    # employee account can edit own availability but not others'
    r = client.post(
        "/api/auth/users",
        json={"email": "worker@x.com", "password": "pw123456", "role": "employee",
              "employee_id": emp["id"]},
        headers=manager_headers,
    )
    assert r.status_code == 200
    tok = client.post("/api/auth/login", json={"email": "worker@x.com", "password": "pw123456"}).json()
    worker_headers = {"Authorization": f"Bearer {tok['token']}"}

    r = client.put(
        f"/api/employees/{emp['id']}/availability",
        json=[{"weekday": 0, "start_min": 360, "end_min": 900}],
        headers=worker_headers,
    )
    assert r.status_code == 200
    assert client.put(
        "/api/employees/99/availability", json=[], headers=worker_headers
    ).status_code == 403
    # manager-only endpoints closed to employees
    assert client.get("/api/ratings", headers=worker_headers).status_code == 403


def test_full_scheduling_flow(client, manager_headers):
    levels = {l["name"]: l for l in client.get("/api/levels", headers=manager_headers).json()}
    k1, b2 = levels["K1"], levels["B2"]

    for name, lvl in [("W One", k1), ("W Two", k1), ("Lead", b2)]:
        assert client.post(
            "/api/employees", json={"name": name, "level_id": lvl["id"]}, headers=manager_headers
        ).status_code == 200
    employees = {e["name"]: e for e in client.get("/api/employees", headers=manager_headers).json()}

    shift = client.post(
        "/api/shifts",
        json={
            "date": MONDAY,
            "start_min": 420,
            "end_min": 780,
            "requirements": [
                {"level_id": k1["id"], "count": 2},
                {"level_id": b2["id"], "count": 2},  # only one B2 exists -> unfilled
            ],
        },
        headers=manager_headers,
    ).json()

    detail = client.post(
        f"/api/schedules/generate?week_start={MONDAY}", headers=manager_headers
    ).json()
    assert len(detail["schedule"]["assignments"]) == 3
    assert detail["unfilled"] == [
        {"shift_id": shift["id"], "level_id": b2["id"], "level_name": "B2", "missing": 1}
    ]
    schedule_id = detail["schedule"]["id"]

    # suggestions for the gap: nobody same-level left, no higher level available
    sug = client.get(
        f"/api/schedules/{schedule_id}/suggestions?shift_id={shift['id']}&level_id={b2['id']}",
        headers=manager_headers,
    ).json()
    assert all(s["softness"] >= 0 for s in sug)

    # manual fill with a K1 would be rejected for B2-only... level must be required on shift
    r = client.post(
        f"/api/schedules/{schedule_id}/assignments",
        json={
            "shift_id": shift["id"],
            "employee_id": employees["W One"]["id"],
            "fills_level_id": b2["id"],
        },
        headers=manager_headers,
    )
    assert r.status_code == 409  # already assigned to this shift

    # publish, then a worker can read it
    assert client.post(
        f"/api/schedules/{schedule_id}/publish", headers=manager_headers
    ).json()["schedule"]["status"] == "published"
    r = client.get(f"/api/schedules/week/{MONDAY}", headers=manager_headers)
    assert r.status_code == 200

    # regenerate keeps manual assignments only for manual=True; here all were auto
    detail2 = client.post(
        f"/api/schedules/generate?week_start={MONDAY}", headers=manager_headers
    ).json()
    assert len(detail2["schedule"]["assignments"]) == 3


def test_copy_week_and_sla_settings(client, manager_headers):
    levels = {l["name"]: l for l in client.get("/api/levels", headers=manager_headers).json()}
    client.post(
        "/api/shifts",
        json={"date": MONDAY, "start_min": 420, "end_min": 780,
              "requirements": [{"level_id": levels["K1"]["id"], "count": 1}]},
        headers=manager_headers,
    )
    copied = client.post(
        "/api/shifts/copy-week",
        json={"from_week": MONDAY, "to_week": "2026-06-22"},
        headers=manager_headers,
    ).json()
    assert len(copied) == 1 and copied[0]["date"] == "2026-06-22"

    slas = client.get("/api/settings/sla", headers=manager_headers).json()
    assert slas[0]["target_seconds"] == 300
    r = client.post(
        "/api/settings/sla",
        json={"target_seconds": 270, "effective_from": "2026-09-01"},
        headers=manager_headers,
    )
    assert r.status_code == 200
    assert len(client.get("/api/settings/sla", headers=manager_headers).json()) == 2
