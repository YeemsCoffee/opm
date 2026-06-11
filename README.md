# OPM — Yeems Coffee Auto-Scheduling

Auto-scheduling for managers: employees enter availability, managers define
per-shift level requirements (e.g. `1× Shift Lead, 1× B2, 2× K1`), and a
CP-SAT solver generates the optimal weekly schedule — biased toward putting
the strongest crew on the busiest shifts via an NBA-style **plus/minus**
rating derived from **ticket time adherence**.

## How plus/minus works

1. **Import data** (Imports page): the Square KDS *kitchen report* (ticket
   open/close times) and the Square Team *timesheets* export (who worked
   when, including breaks). Employees and their levels are created/updated
   automatically from timesheets. Re-imports are idempotent.
2. Every ticket is judged against the **effective-dated close target**
   (default 5:00 — change it in Settings; history is never rewritten).
3. For every ticket created while an employee was on the floor (clocked in,
   not on break, not in a Training role), the store's on-time rate is
   compared to the expected rate for that hour of day. The employee's raw
   +/- is the mean residual in percentage points: *"adherence runs +2.3
   points higher when they're working."*
4. Small samples are **shrunk toward 0** (configurable) so one good week
   doesn't crown anyone; the dashboard flags low-confidence sample sizes.

## How scheduling works

- **Hard rules** (never bent automatically): availability windows, time
  off, exact level match per slot, no overlapping shifts, minimum rest
  between days, weekly max hours.
- **Objective**, in strict priority: fill every slot → maximize team +/-
  weighted by each shift's forecast ticket demand → keep hours close to
  each employee's target.
- **Unfillable slots stay visibly empty** on the board. Clicking one shows
  ranked suggestions, each labeled with the exact rule it would bend
  (higher level covering down ▸ over weekly hours ▸ outside availability).
  Assigning is always the manager's call; manual picks (✎) survive
  re-generation.
- Publish makes the week visible to employee accounts.

## Running it

```bash
# backend (Python 3.11+)
cd backend
python3 -m venv ../.venv && ../.venv/bin/pip install -r requirements.txt
../.venv/bin/uvicorn app.main:app --port 8000

# frontend (build once; FastAPI serves frontend/dist at /)
cd frontend
npm install && npm run build
```

Open http://localhost:8000 and create the **first manager account** (open
registration closes after the first user; managers create further accounts
and can link employee accounts via `employee_id`).

For frontend development with hot reload: `npm run dev` (proxies `/api` to
`:8000`).

Storage is SQLite (`backend/opm.db`) by default; set `DATABASE_URL` for
Postgres. Set `OPM_SECRET` in production for stable auth tokens.

```bash
# tests
cd backend && ../.venv/bin/python -m pytest tests/
```

## Weekly workflow

1. Employees keep availability/time-off current (their own login → *My
   availability*). No windows entered = treated as fully available,
   flagged "unconfirmed".
2. Manager: *Schedule* → **Copy last week's shifts** (or add/edit shifts
   and their level requirements) → **Generate** → resolve any highlighted
   gaps via suggestions → **Publish**.
3. After each payroll period, import the new kitchen report + timesheets
   to keep ratings fresh.

## Project layout

```
backend/app/
  models.py            # SQLAlchemy schema (employees, levels, shifts, tickets…)
  routers/             # FastAPI endpoints (auth, employees, shifts, schedules…)
  services/
    kitchen_import.py  # Square KDS ticket CSV importer
    timesheet_import.py# Square Team timesheet importer (sessions, breaks, no-shows)
    ratings.py         # plus/minus engine (hour-of-day baseline + shrinkage)
    demand.py          # tickets-per-hour forecast for shift weighting
    solver.py          # OR-Tools CP-SAT schedule generation
    suggestions.py     # ranked candidates for unfilled slots
backend/tests/         # importer, ratings, solver and API tests
frontend/src/pages/    # React UI (schedule board, employees, ratings…)
```

## Roadmap

- Adjusted +/- via ridge regression (separates individuals who always work
  together; raw on/off +/- inherits crewmate effects).
- Shift templates beyond copy-week; notifications on publish.
- Direct Square API sync instead of CSV import.
