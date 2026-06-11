"""Plus/minus ratings from ticket time adherence.

For every ticket created while an employee was on the floor (clocked in,
not on break, in a rating-eligible role), compare the store's on-time rate
to the expected rate for that hour of day. An employee's raw +/- is the
mean residual in percentage points; the published +/- is shrunk toward 0
for small samples so one good week doesn't crown anyone.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Level, SlaConfig, SolverConfig, Ticket, WorkSession


def sla_target_for(configs: list[SlaConfig], at: datetime) -> SlaConfig | None:
    """Configs must be sorted by effective_from ascending."""
    current = None
    for cfg in configs:
        if cfg.effective_from <= at.date():
            current = cfg
        else:
            break
    return current


def load_sla_configs(db: Session) -> list[SlaConfig]:
    return list(db.scalars(select(SlaConfig).order_by(SlaConfig.effective_from)))


def _tickets_in_range(db: Session, start: date, end: date) -> list[Ticket]:
    return list(
        db.scalars(
            select(Ticket).where(
                Ticket.created_at >= datetime.combine(start, time.min),
                Ticket.created_at < datetime.combine(end + timedelta(days=1), time.min),
                Ticket.recalled.is_(False),
            )
        )
    )


def compute_ratings(db: Session, start: date, end: date) -> list[dict]:
    configs = load_sla_configs(db)
    solver_cfg = db.scalar(select(SolverConfig))
    shrink_k = solver_cfg.shrinkage_tickets if solver_cfg else 300

    tickets = _tickets_in_range(db, start, end)
    sessions = list(
        db.scalars(
            select(WorkSession)
            .join(Level, WorkSession.level_id == Level.id)
            .options(selectinload(WorkSession.breaks), selectinload(WorkSession.employee))
            .where(
                Level.counts_for_rating.is_(True),
                WorkSession.clock_in < datetime.combine(end + timedelta(days=1), time.min),
                WorkSession.clock_out > datetime.combine(start, time.min),
            )
        )
    )
    if not tickets:
        return []

    on_time = {}
    for t in tickets:
        cfg = sla_target_for(configs, t.created_at)
        target = cfg.target_seconds if cfg else 300
        on_time[t.id] = t.completion_seconds <= target

    # hour-of-day baseline
    by_hour = defaultdict(lambda: [0, 0])
    for t in tickets:
        by_hour[t.created_at.hour][0] += on_time[t.id]
        by_hour[t.created_at.hour][1] += 1
    baseline = {h: c[0] / c[1] for h, c in by_hour.items()}

    # bucket sessions by date so each ticket only scans that day's crew
    by_day = defaultdict(list)
    for s in sessions:
        d = s.clock_in.date()
        while d <= s.clock_out.date():
            by_day[d].append(s)
            d += timedelta(days=1)

    stats = defaultdict(lambda: {"on": 0, "n": 0, "exp": 0.0})
    shift_hits = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # emp -> session -> [on, n]
    for t in tickets:
        for s in by_day.get(t.created_at.date(), []):
            if s.on_floor(t.created_at):
                st = stats[s.employee_id]
                st["on"] += on_time[t.id]
                st["n"] += 1
                st["exp"] += baseline[t.created_at.hour]
                sh = shift_hits[s.employee_id][s.id]
                sh[0] += on_time[t.id]
                sh[1] += 1

    emp_meta = {s.employee_id: s for s in sessions}
    goal = configs[-1].adherence_goal if configs else 0.9
    out = []
    for emp_id, st in stats.items():
        if st["n"] == 0:
            continue
        actual = st["on"] / st["n"]
        expected = st["exp"] / st["n"]
        raw = (actual - expected) * 100
        shrunk = raw * st["n"] / (st["n"] + shrink_k)
        shifts = shift_hits[emp_id]
        hit = sum(1 for on, n in shifts.values() if n > 0 and on / n >= goal)
        sess = emp_meta[emp_id]
        out.append(
            {
                "employee_id": emp_id,
                "employee_name": sess.employee.name,
                "level_name": sess.employee.level_on(end).name if sess.employee.level_on(end) else "",
                "tickets": st["n"],
                "on_floor_adherence": round(actual, 4),
                "expected_adherence": round(expected, 4),
                "raw_plus_minus": round(raw, 2),
                "plus_minus": round(shrunk, 2),
                "shifts_hit_target": hit,
                "shifts_total": len(shifts),
            }
        )
    out.sort(key=lambda r: r["plus_minus"], reverse=True)
    return out


def adherence_what_if(db: Session, target_seconds: int, start: date, end: date) -> dict:
    tickets = _tickets_in_range(db, start, end)
    n = len(tickets)
    on = sum(1 for t in tickets if t.completion_seconds <= target_seconds)
    return {
        "target_seconds": target_seconds,
        "start": start,
        "end": end,
        "tickets": n,
        "adherence": round(on / n, 4) if n else 0.0,
    }
