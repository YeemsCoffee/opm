"""Entry-time feature assembly.

Everything here is computed from information available **when the order
fills** — formation geometry, prior-wick metrics, displacement, pre-entry
efficiency and overlap, zone interaction counts, context-level distances,
target quality, and contract/roll context.

Retrospective range-onset labels live in
:func:`fvg_backtest.analytics.trade_metrics.range_onset` and are deliberately
kept out of this module so they cannot leak into a model's feature set.
:data:`ENTRY_TIME_FEATURES` is the leak-safe allowlist used for training.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl

from ..config.schema import AppConfig
from ..execution.costs import CostModel
from ..liquidity.context import compute_context_levels, target_matches_context
from ..sessions.clock import SessionClock
from .indicators import candle_overlap, efficiency_ratio

# feature families safe to train on (prefix match)
ENTRY_TIME_FEATURE_PREFIXES = (
    "fvg_",
    "type_a_",
    "type_b_",
    "displacement_",
    "pre_entry_",
    "zone_before_entry_",
    "target_",
    "ctx_",
    "rel_",
    "opening_range_",
    "overnight_range_",
    "entry_",
    "formation_",
    "contract_",
    "volume_",
    "session_",
)

ENTRY_TIME_FEATURES = ENTRY_TIME_FEATURE_PREFIXES  # alias kept for clarity


def is_entry_time_feature(name: str) -> bool:
    return name.startswith(ENTRY_TIME_FEATURE_PREFIXES)


def build_setup_features(
    result,
    session_bars: pl.DataFrame,
    prior_session_bars: pl.DataFrame | None,
    clock: SessionClock,
    config: AppConfig,
    costs: CostModel,
) -> dict:
    """Feature row for the session's selected setup (empty when none)."""
    fvg = result.selected
    if fvg is None:
        return {}

    rows = session_bars.to_dicts()
    formation_index = fvg.c3_index
    formation_time = fvg.c3_time
    atr = fvg.atr_at_formation or 0.0
    cash_open = clock.cash_open_dt(result.session_date)

    zone = result.zone
    first_order = result.orders[0] if result.orders else None
    first_trade = result.trades[0] if result.trades else None
    entry_time = first_trade.entry_time if first_trade else None
    entry_price = first_trade.order.entry if first_trade else (
        first_order.entry if first_order else fvg.proximal
    )
    decision_time = entry_time or formation_time
    decision_index = _index_at(rows, decision_time)

    ctx = compute_context_levels(
        session_bars, prior_session_bars, clock, config.context, decision_time,
        reference_price=entry_price,
    )
    out: dict = {
        "session_date": result.session_date,
        "formation_time": formation_time,
        "formation_minute_of_session": int(
            (formation_time - cash_open).total_seconds() // 60
        ),
        "formation_hour": formation_time.hour,
        "entry_time": entry_time,
        "entry_minute_of_session": (
            int((entry_time - cash_open).total_seconds() // 60) if entry_time else None
        ),
        "entry_delay_minutes": (
            int((entry_time - formation_time).total_seconds() // 60) if entry_time else None
        ),
        "session_day_of_week": result.session_date.strftime("%A"),
        "session_month": result.session_date.month,
        "session_iso_week": result.session_date.isocalendar().week,
        "fvg_direction": str(fvg.direction),
        "fvg_significance_type": fvg.significance_type,
        "fvg_gap_points": fvg.gap_width,
        "fvg_gap_ticks": fvg.gap_width / costs.tick if costs.tick else None,
        "fvg_gap_atr": fvg.type_a.normalized_gap,
        "fvg_preservation_ratio": fvg.type_a.preservation_ratio,
        "fvg_atr_at_formation": atr,
        "fvg_midpoint": fvg.midpoint,
        "fvg_low": fvg.fvg_low,
        "fvg_high": fvg.fvg_high,
    }
    out.update(fvg.type_b.to_dict())
    out.update({k: v for k, v in fvg.displacement.items()})

    # -- pre-entry path behaviour (formation -> decision) -------------------
    pre = rows[formation_index : max(decision_index, formation_index) + 1]
    closes = np.array([r["close"] for r in pre], dtype=float)
    highs = np.array([r["high"] for r in pre], dtype=float)
    lows = np.array([r["low"] for r in pre], dtype=float)
    for window in config.range_research.efficiency_windows:
        seg = closes[-window:] if len(closes) >= 2 else closes
        out[f"pre_entry_efficiency_{window}"] = (
            efficiency_ratio(seg) if len(seg) >= 2 else None
        )
    out["pre_entry_overlap"] = candle_overlap(highs, lows) if len(highs) > 1 else None
    out["pre_entry_bars"] = len(pre)

    # -- zone interaction strictly before the entry -------------------------
    if zone is not None:
        before = [e for e in zone.events if entry_time is None or e.timestamp < entry_time]
        out.update(
            {
                "zone_before_entry_mitigations": sum(
                    1 for e in before if e.kind == "MITIGATION"
                ),
                "zone_before_entry_closes_inside": sum(
                    1 for e in before if e.kind == "CLOSE_INSIDE"
                ),
                "zone_before_entry_inversions": sum(
                    1 for e in before if e.kind in ("INVERSION", "REINVERSION")
                ),
                "zone_before_entry_wicks_through": sum(
                    1 for e in before if e.kind == "WICK_THROUGH_ZONE"
                ),
            }
        )
        out.update(zone.summary())

    # -- volume relative to the same minute historically --------------------
    formation_bar = rows[formation_index]
    same_time = [
        r["volume"]
        for r in rows
        if abs((r["timestamp_ny"] - cash_open).total_seconds()) < 3600
        and r["timestamp_ny"] <= formation_time
    ]
    median_vol = float(np.median(same_time)) if same_time else None
    out["volume_at_formation"] = formation_bar.get("volume")
    out["volume_vs_session_median"] = (
        formation_bar["volume"] / median_vol if median_vol else None
    )

    # -- context levels ------------------------------------------------------
    out.update(ctx.to_dict())
    out.update(ctx.relative_features(entry_price, atr))
    out["opening_range_position"] = ctx.opening_range_position(entry_price)
    out["overnight_range_position"] = ctx.overnight_range_position(entry_price)
    out["opening_range_size_atr"] = (
        ctx.opening_range_long_size / atr if ctx.opening_range_long_size and atr else None
    )
    out["developing_range_atr"] = (
        ctx.developing_range / atr if ctx.developing_range and atr else None
    )
    out["entry_vs_open_price_points"] = (
        entry_price - ctx.open_price if ctx.open_price is not None else None
    )
    out["direction_vs_overnight_range"] = _direction_vs_range(
        str(fvg.direction), entry_price, ctx.overnight_high, ctx.overnight_low
    )
    out["direction_vs_opening_range"] = _direction_vs_range(
        str(fvg.direction), entry_price, ctx.opening_range_long_high, ctx.opening_range_long_low
    )

    # -- target quality -------------------------------------------------------
    sel = result.target_selections[0] if result.target_selections else None
    risk = first_order.risk_points if first_order else 0.0
    if sel is not None:
        out.update(
            sel.to_dict(
                now=decision_time, entry=entry_price, tick=costs.tick, atr=atr, risk=risk
            )
        )
        if sel.found and sel.price is not None:
            out.update(target_matches_context(sel.price, ctx, costs.tick))
    out["entry_model"] = config.entries.model
    out["inversion_stop_model"] = config.inversion.stop_model
    out["execution_mode"] = config.execution.mode

    # -- opening-range overlap flag for the trade ----------------------------
    if entry_time is not None:
        or_end = cash_open + timedelta(minutes=config.context.opening_range_minutes_long)
        out["overlapped_opening_range"] = entry_time < or_end
        out["open_price"] = ctx.open_price
    return out


def _index_at(rows: list[dict], when) -> int:
    for i, r in enumerate(rows):
        if r["timestamp_ny"] >= when:
            return i
    return len(rows) - 1


def _direction_vs_range(
    direction: str, price: float, high: float | None, low: float | None
) -> str | None:
    if high is None or low is None:
        return None
    if price > high:
        return "ABOVE_RANGE"
    if price < low:
        return "BELOW_RANGE"
    mid = (high + low) / 2
    return "UPPER_HALF" if price >= mid else "LOWER_HALF"
