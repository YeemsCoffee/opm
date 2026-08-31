"""Break schedule engine.

Given the day's roster (who works which hours), assign each person their
entitled breaks — rest breaks (paid) and meal breaks (unpaid) per the
configurable rules — staggered so that:
  - at most `max_concurrent` people are on break in any 5-minute slot
  - breaks stay away from shift edges (`edge_pad_minutes`)
  - one person's breaks are at least `min_gap_minutes` apart, in order
  - meals start before `meal_by_minute` into the shift (CA: 5th hour)
  - breaks land in ticket-demand lulls, near natural anchor points
    (quarter / mid / three-quarter of the shift)
"""

from dataclasses import dataclass

from ortools.sat.python import cp_model
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BreakConfig, BreakPlanItem, BreakRule, RosterEntry
from .demand import hourly_averages

SLOT = 5  # minutes
SOLVER_TIME_LIMIT_S = 10


@dataclass
class PlannedBreak:
    roster_entry_id: int
    kind: str  # rest | meal
    start_min: int
    end_min: int
    paid: bool


def entitlement(shift_minutes: int, rules: list[BreakRule]) -> tuple[int, int]:
    """(rest_breaks, meal_breaks) — the longest matching rule wins."""
    best = None
    for r in sorted(rules, key=lambda r: r.min_shift_minutes):
        if shift_minutes >= r.min_shift_minutes:
            best = r
    return (best.rest_breaks, best.meal_breaks) if best else (0, 0)


def _demand_slot_costs(db: Session) -> dict[int, int]:
    """Cost per 5-min slot of day from historical ticket demand (0 if none)."""
    hourly = hourly_averages(db)
    return {
        slot: round(hourly.get(slot * SLOT // 60, 0.0) * 10)
        for slot in range(0, 1440 // SLOT)
    }


def solve_breaks(db: Session, roster: list[RosterEntry]) -> list[PlannedBreak]:
    cfg = db.scalar(select(BreakConfig))
    rules = list(db.scalars(select(BreakRule)))
    slot_cost = _demand_slot_costs(db)

    model = cp_model.CpModel()
    all_intervals = []
    break_vars = []  # (entry, kind, start_var, dur_min, lo_slot, cost_var, anchor_dev)

    for entry in roster:
        length = entry.end_min - entry.start_min
        rest_n, meal_n = entitlement(length, rules)
        if rest_n + meal_n == 0:
            continue

        # breaks in shift order with their anchor points
        plan: list[tuple[str, int, float]] = []  # (kind, dur, anchor_frac)
        if rest_n >= 1 and meal_n >= 1:
            plan.append(("rest", cfg.rest_minutes, 0.25))
            plan.append(("meal", cfg.meal_minutes, 0.5))
            if rest_n >= 2:
                plan.append(("rest", cfg.rest_minutes, 0.75))
        elif meal_n >= 1:
            plan.append(("meal", cfg.meal_minutes, 0.5))
        else:
            anchors = {1: [0.5], 2: [0.33, 0.75]}.get(rest_n, [0.5] * rest_n)
            for a in anchors[:rest_n]:
                plan.append(("rest", cfg.rest_minutes, a))

        prev_end = None
        for kind, dur, frac in plan:
            lo = entry.start_min + cfg.edge_pad_minutes
            hi = entry.end_min - cfg.edge_pad_minutes - dur
            if kind == "meal":
                hi = min(hi, entry.start_min + cfg.meal_by_minute)
            lo_slot, hi_slot = -(-lo // SLOT), hi // SLOT
            if hi_slot < lo_slot:
                continue  # shift too short to place this break cleanly

            start = model.new_int_var(lo_slot, hi_slot, f"b_{entry.id}_{kind}_{len(break_vars)}")
            interval = model.new_fixed_size_interval_var(start, dur // SLOT, f"iv_{len(break_vars)}")
            all_intervals.append(interval)

            if prev_end is not None:
                model.add(start >= prev_end + cfg.min_gap_minutes // SLOT)
            prev_end_var = model.new_int_var(0, 1440 // SLOT, f"e_{len(break_vars)}")
            model.add(prev_end_var == start + dur // SLOT)
            prev_end = prev_end_var

            # demand cost of this placement
            costs = [
                sum(slot_cost.get(s + k, 0) for k in range(dur // SLOT))
                for s in range(lo_slot, hi_slot + 1)
            ]
            cost = model.new_int_var(0, max(costs) if costs else 0, f"c_{len(break_vars)}")
            idx = model.new_int_var(0, hi_slot - lo_slot, f"i_{len(break_vars)}")
            model.add(idx == start - lo_slot)
            model.add_element(idx, costs, cost)

            anchor_slot = round((entry.start_min + length * frac) / SLOT)
            dev = model.new_int_var(0, 1440 // SLOT, f"d_{len(break_vars)}")
            model.add(dev >= start - anchor_slot)
            model.add(dev >= anchor_slot - start)

            break_vars.append((entry, kind, start, dur, cost, dev))

    if not break_vars:
        return []

    model.add_cumulative(all_intervals, [1] * len(all_intervals), cfg.max_concurrent)
    model.minimize(
        10 * sum(c for *_, c, _d in break_vars) + sum(d for *_, d in break_vars)
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT_S
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # fall back: drop the concurrency coupling and anchor each break alone
        return _fallback(roster, cfg, rules)

    out = []
    for entry, kind, start, dur, _c, _d in break_vars:
        start_min = solver.value(start) * SLOT
        out.append(
            PlannedBreak(
                roster_entry_id=entry.id,
                kind=kind,
                start_min=start_min,
                end_min=start_min + dur,
                paid=(kind == "rest"),
            )
        )
    return out


def _fallback(roster, cfg, rules) -> list[PlannedBreak]:
    out = []
    for entry in roster:
        length = entry.end_min - entry.start_min
        rest_n, meal_n = entitlement(length, rules)
        fracs = [(0.25, "rest")] * min(rest_n, 1) + [(0.5, "meal")] * meal_n
        if rest_n >= 2:
            fracs.append((0.75, "rest"))
        for frac, kind in fracs:
            dur = cfg.rest_minutes if kind == "rest" else cfg.meal_minutes
            start = int(entry.start_min + length * frac)
            start = min(max(start, entry.start_min + cfg.edge_pad_minutes), entry.end_min - cfg.edge_pad_minutes - dur)
            if kind == "meal":
                start = min(start, entry.start_min + cfg.meal_by_minute)
            if start < entry.start_min or start + dur > entry.end_min:
                continue
            out.append(PlannedBreak(entry.id, kind, start, start + dur, kind == "rest"))
    return out


def apply_break_plan(db: Session, roster: list[RosterEntry], plan: list[PlannedBreak]) -> None:
    for entry in roster:
        for item in list(entry.breaks):
            db.delete(item)
    db.flush()
    for b in plan:
        db.add(
            BreakPlanItem(
                roster_entry_id=b.roster_entry_id,
                kind=b.kind,
                start_min=b.start_min,
                end_min=b.end_min,
                paid=b.paid,
            )
        )
    db.commit()
