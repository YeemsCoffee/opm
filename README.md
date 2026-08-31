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

- **Weekly pattern**: managers define reusable **shift blocks** (name +
  start/end time, e.g. "Open" 6:30–2:30) and place them on weekdays with
  per-level headcounts ("Monday needs 1 Open: 1× Shift Lead, 1× B2, 2× K1").
  Every future week is generated from this pattern until it's changed;
  weeks already generated or hand-edited keep their own shifts.
- **Hard rules** (never bent automatically): availability windows, time
  off, exact level match per slot, no overlapping shifts, minimum rest
  between days, max hours per day and per week, and no 7th consecutive
  worked day — rolling across week boundaries, seeded from timesheets and
  prior schedules. The daily/weekly/consecutive limits are configurable in
  Settings.
- **Objective**, in strict priority: fill every slot → maximize team +/-
  weighted by each shift's forecast ticket demand → keep hours close to
  each employee's target.
- **Unfillable slots stay visibly empty** on the board. Clicking one shows
  ranked suggestions, each labeled with the exact rule it would bend
  (higher level covering down ▸ overtime ▸ 7th consecutive day ▸ outside
  availability). Assigning is always the manager's call; manual picks (✎)
  survive re-generation, and any limit they cross is flagged with ⚠ on the
  schedule.
- Publish makes the week visible to employee accounts.

Employees also carry manager-defined **skills** (dialing, steaming, …) as
checkboxes, and per-day-of-week availability windows.

## Break schedules

The **Breaks** page builds the day's break schedule. Load the roster three
ways — **Pull from Homebase**, **From app schedule** (this app's own
published schedule), or by typing people in — then **Generate breaks**.

A CP-SAT solver places each person's entitled breaks so that:

- entitlement follows configurable rules by shift length (seeded from your
  timesheets: 3.5h+ → one paid 10; 6h+ → two paid 10s and an unpaid 30)
- **never more than one person on break at a time** (configurable), so the
  floor is always covered
- breaks stay clear of shift start/end (45 min default) and are spaced at
  least 90 min apart for the same person
- **meal breaks start before the 5th hour ends** (California-style rule)
- breaks land in **ticket-demand lulls** — the same hourly demand model the
  scheduler uses, so nobody goes on break during the 8–10am rush

Managers can drag any break to a different time (it must stay inside the
shift) and print the result for the bar.

### Homebase connection

Homebase issues API keys through their support/sales team on the Enterprise
plan — there is no self-serve signup and no public MCP server, so the
integration reads credentials from the environment:

```
HOMEBASE_API_KEY=...          # key Homebase issues you
HOMEBASE_LOCATION_UUID=...    # your location's UUID
HOMEBASE_API_BASE=...         # optional, defaults to https://api.joinhomebase.com
```

Until those are set, the "Pull from Homebase" button is disabled and the
other two roster sources work normally. The response parser is defensive
about field names (`start_at`/`start_time`/`starts_at`, etc.) since the
exact payload can only be confirmed against a live key.

## Homebase browser sync (live hours + shift swaps)

Without an Enterprise Homebase account, there's no API for two other things
managers need automated: **hours worked per employee, updated daily**, and
**who picked up shifts released for coverage**. Since there's no public
Homebase MCP server either, this reads the same pages a manager would look
at by hand — via a real, Playwright-driven browser — on a schedule, instead
of anyone uploading anything.

**Read this before using it:** automating a login to a third-party
dashboard instead of using its API is very likely against Homebase's Terms
of Service, and can get an account flagged, rate-limited, or locked. This
was a deliberate, discussed trade-off, not an oversight — consider using a
dedicated Homebase login for this rather than your primary one.

**How it avoids storing a password:** you log into Homebase yourself, once,
in a real visible browser window that the setup script opens **on the same
machine that will run the daily sync** (your PC — not a remote/cloud
session, which would lose the session when it's reclaimed). That session
is saved to a local browser profile folder; nothing about your password is
ever written down, only what a browser itself keeps after a normal login.

```powershell
cd backend
..\.venv\Scripts\python scripts\homebase_login.py
```

A Chrome window opens — log in normally, then close it (or let it close
itself once login is detected).

**Daily sync**, reusing that saved session, never logging in on its own:

```powershell
..\.venv\Scripts\python scripts\homebase_sync.py
```

Wire this to **Windows Task Scheduler** to run once a day automatically:
Task Scheduler → Create Task → Trigger: Daily, whatever time → Action:
Start a program → Program: the full path to
`...\opm\.venv\Scripts\python.exe` → Arguments:
`scripts\homebase_sync.py` → Start in: the full path to `...\opm\backend`.

If the saved session expires, the sync stops touching data and reports it
clearly — check the banner at the top of the Employees page, or the
`/api/homebase-sync/status` endpoint — rather than guessing. Re-run the
login script when that happens.

**What each page's scrape does and doesn't need calibrating:**

- **Hours (Timesheets report)** — confirmed working against a real
  Homebase screenshot. The total is embedded in the "Worked" cell as
  `Total: 8 hrs 20 min`; no-show rows (no "Total:" line) are correctly
  skipped rather than counted as zero hours. Feeds a live "Hours
  (Homebase)" column on the Employees page.
- **Shift swaps** — confirmed against real screenshots: there's no
  separate trade-board report. A covered shift shows as a flagged cell
  (orange, warning icon, "Open Shift approved") directly in the normal
  weekly Schedule Builder grid, under the covering employee's row —
  confirmed that's all that's needed (no click-through for who originally
  released it). Since this page is a visual calendar grid rather than a
  table, it's scraped by **position, not DOM structure**: every flagged
  cell is matched to whichever day-column header sits closest on the x
  axis and whichever employee name sits closest on the y axis
  (`services/homebase_grid_parser.py`, unit-tested with synthetic
  coordinates). That's deliberately more resilient to a Homebase layout
  change than a class-name or table selector would be. Feeds the **Shift
  Swaps** page. **A failure here never blocks or discards the hours
  sync** — the two are scraped and persisted independently.

Everything the scraper reads from (URLs, column-header keywords) lives in
`backend/homebase_scrape_config.json`, not code, so recalibrating after a
Homebase layout change is a config edit, not a redeploy.

One-time setup on the machine that will run the sync:

```powershell
cd backend
..\.venv\Scripts\pip install -r requirements.txt
..\.venv\Scripts\playwright install chromium
```

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
2. Manager: *Shift pattern* → define blocks and place them on weekdays
   (once). Then weekly: *Schedule* → **Generate** → resolve any
   highlighted gaps via suggestions → **Publish**.
3. After each payroll period, import the new kitchen report + timesheets
   to keep ratings fresh.
4. Whenever the roster changes (new hires, location moves), import
   Homebase's Team export (Imports page) to create/update employees and
   assign locations. It never overwrites an employee's level once
   established from timesheets — only location and brand-new employees'
   levels come from this file.

## Project layout

```
backend/app/
  models.py            # SQLAlchemy schema (employees, levels, shifts, tickets…)
  routers/             # FastAPI endpoints (auth, employees, shifts, schedules…)
  services/
    kitchen_import.py  # Square KDS ticket CSV importer
    timesheet_import.py# Square Team timesheet importer (sessions, breaks, no-shows)
    team_import.py     # Homebase Team roster importer (employees + locations)
    ratings.py         # plus/minus engine (hour-of-day baseline + shrinkage)
    demand.py          # tickets-per-hour forecast for shift weighting
    solver.py          # OR-Tools CP-SAT schedule generation
    suggestions.py     # ranked candidates for unfilled slots
    breaks.py          # CP-SAT break scheduling (staggered, demand-aware)
    homebase.py        # Homebase API connector for the day's roster (Enterprise plan)
    homebase_browser.py       # Playwright driver: reuses a saved login session
    homebase_table_parser.py  # pure HTML-table parsing (hours) — unit-tested without a browser
    homebase_grid_parser.py   # pure position-based grid parsing (shift swaps) — same
  scripts/
    homebase_login.py  # one-time: opens a real window for a human to log in
    homebase_sync.py    # run daily by Task Scheduler; never logs in itself
backend/homebase_scrape_config.json  # URLs + column/marker keywords the sync reads — edit, don't redeploy
backend/tests/         # importer, ratings, solver and API tests
frontend/src/pages/    # React UI (schedule board, employees, shift swaps, ratings…)
```

## Roadmap

- Adjusted +/- via ridge regression (separates individuals who always work
  together; raw on/off +/- inherits crewmate effects).
- Per-shift skill requirements (skills are currently informational).
- Notifications on publish.
- Direct Square API sync instead of CSV import.
