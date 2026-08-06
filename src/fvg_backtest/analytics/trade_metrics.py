"""Trade-path metrics and quality labels.

Every metric is derived from the path the simulator actually assumed, so
MAE/MFE can never contradict the resolved event sequence.  R is the primary
normalized measure: ``gross_points / risk_points``.

Labels (all thresholds configurable):

``CLEAN_WIN``        target first, shallow MAE, quick to 0.5R, short duration
``SWEATY_WIN``       a win that met >= N of the stress conditions
``IMMEDIATE_FAILURE`` stopped before MFE ever reached 0.25R
``STALLED``          no resolution and MFE < 0.5R after N minutes
``RANGING``          >= N range conditions met

Every underlying condition is stored alongside the label so the research can
question the label definition itself.
"""

from __future__ import annotations

import numpy as np

from ..config.schema import LabelConfig, RangeResearchConfig
from ..execution.costs import CostModel
from ..execution.simulator import TradeRecord
from ..features.indicators import candle_overlap, efficiency_ratio


def _signed(direction: str, exit_price: float, entry: float) -> float:
    return exit_price - entry if direction == "LONG" else entry - exit_price


def _favorable(direction: str, price: float, entry: float) -> float:
    return price - entry if direction == "LONG" else entry - price


def compute_trade_metrics(
    trade: TradeRecord, costs: CostModel, labels: LabelConfig, quantity: int = 1
) -> dict:
    order = trade.order
    direction = order.direction
    entry = order.entry                       # the level, for gross accounting
    risk = order.risk_points
    tick = costs.tick

    gross_exit = trade.gross_exit_price if trade.gross_exit_price is not None else trade.exit_price
    gross_points = _signed(direction, gross_exit, entry)
    net_points = _signed(direction, trade.exit_price, trade.entry_price) - costs.fees_in_points(
        quantity
    )
    r = gross_points / risk if risk > 0 else 0.0
    net_r = net_points / risk if risk > 0 else 0.0

    # -- excursions ----------------------------------------------------------
    mfe = mae = 0.0
    t_quarter = t_half = t_one = None
    atr = order.context.get("atr_at_formation") or (
        trade.path[0].get("atr") if trade.path else None
    ) or 0.0
    for step in trade.path:
        lo, hi = step["excursion_low"], step["excursion_high"]
        if lo is None or hi is None:
            continue
        best = _favorable(direction, hi if direction == "LONG" else lo, entry)
        worst = _favorable(direction, lo if direction == "LONG" else hi, entry)
        mfe = max(mfe, best)
        mae = min(mae, worst)
        minutes = int((step["timestamp"] - trade.entry_time).total_seconds() // 60)
        if risk > 0:
            if t_quarter is None and best >= 0.25 * risk:
                t_quarter = minutes
            if t_half is None and best >= 0.50 * risk:
                t_half = minutes
            if t_one is None and best >= 1.00 * risk:
                t_one = minutes
    mae = abs(mae)

    duration = (
        int((trade.exit_time - trade.entry_time).total_seconds() // 60)
        if trade.exit_time
        else None
    )

    # -- path behaviour -------------------------------------------------------
    closes = np.array([s["close"] for s in trade.path], dtype=float)
    highs = np.array([s["high"] for s in trade.path], dtype=float)
    lows = np.array([s["low"] for s in trade.path], dtype=float)
    entry_crossings = _crossings(closes, entry)
    midpoint = order.context.get("zone_midpoint")
    midpoint_crossings = _crossings(closes, midpoint) if midpoint is not None else 0
    zone_low = order.context.get("zone_low")
    zone_high = order.context.get("zone_high")
    closes_inside = (
        int(np.sum((closes >= zone_low) & (closes <= zone_high)))
        if zone_low is not None and zone_high is not None
        else 0
    )
    direction_changes = _direction_changes(closes)
    overlap = candle_overlap(highs, lows) if len(highs) > 1 else 0.0
    eff10 = efficiency_ratio(closes[:10]) if len(closes) >= 2 else 0.0

    returned_to_entry_after_half = _returned_to_entry_after_half(trade, direction, entry, risk)
    approached_target = _approached_target(trade, direction, order.target, tick)

    metrics = {
        "gross_points": gross_points,
        "net_points": net_points,
        "gross_ticks": gross_points / tick if tick else 0.0,
        "net_ticks": net_points / tick if tick else 0.0,
        "gross_dollars": costs.points_to_dollars(gross_points, quantity),
        "net_dollars": costs.net_dollars(
            _signed(direction, trade.exit_price, trade.entry_price), quantity
        ),
        "result_r": r,
        "net_result_r": net_r,
        "win": gross_points > 0,
        "mfe_points": mfe,
        "mae_points": mae,
        "mfe_ticks": mfe / tick if tick else 0.0,
        "mae_ticks": mae / tick if tick else 0.0,
        "mfe_atr": mfe / atr if atr else None,
        "mae_atr": mae / atr if atr else None,
        "mfe_r": mfe / risk if risk > 0 else 0.0,
        "mae_r": mae / risk if risk > 0 else 0.0,
        "minutes_to_quarter_r": t_quarter,
        "minutes_to_half_r": t_half,
        "minutes_to_one_r": t_one,
        "minutes_to_target": duration if trade.exit_reason == "TARGET" else None,
        "minutes_to_stop": duration if trade.exit_reason == "STOP" else None,
        "duration_minutes": duration,
        "entry_crossings": entry_crossings,
        "midpoint_crossings": midpoint_crossings,
        "closes_inside_zone": closes_inside,
        "mitigations_before_entry": trade.mitigations_before_entry,
        "mitigations_after_entry": trade.mitigations_after_entry,
        "max_penetration": order.context.get("max_penetration"),
        "direction_changes": direction_changes,
        "returned_to_entry_after_half_r": returned_to_entry_after_half,
        "approached_target_within_one_tick": approached_target,
        "inversion_while_open": trade.inversion_while_open,
        "overlapped_opening_range": order.context.get("overlapped_opening_range"),
        "crossed_open_price": _crossed_level(closes, order.context.get("open_price")),
        "post_entry_overlap": overlap,
        "efficiency_ratio_10": eff10,
        "ambiguous_execution": trade.ambiguous_events > 0,
        "ambiguous_event_count": trade.ambiguous_events,
        "had_data_gap": trade.had_data_gap,
        "exit_reason": trade.exit_reason,
        "bars_in_trade": len(trade.path),
    }
    metrics.update(label_trade(metrics, labels))
    return metrics


def _crossings(closes: np.ndarray, level: float | None) -> int:
    if level is None or len(closes) < 2:
        return 0
    side = np.sign(closes - level)
    side = side[side != 0]
    if len(side) < 2:
        return 0
    return int(np.sum(side[1:] != side[:-1]))


def _direction_changes(closes: np.ndarray) -> int:
    if len(closes) < 3:
        return 0
    steps = np.sign(np.diff(closes))
    steps = steps[steps != 0]
    if len(steps) < 2:
        return 0
    return int(np.sum(steps[1:] != steps[:-1]))


def _crossed_level(closes: np.ndarray, level: float | None) -> bool | None:
    if level is None or len(closes) == 0:
        return None
    return bool(np.any(closes > level) and np.any(closes < level))


def _returned_to_entry_after_half(trade, direction, entry, risk) -> bool:
    if risk <= 0:
        return False
    reached = False
    for step in trade.path:
        hi, lo = step["excursion_high"], step["excursion_low"]
        if hi is None or lo is None:
            continue
        best = _favorable(direction, hi if direction == "LONG" else lo, entry)
        worst = _favorable(direction, lo if direction == "LONG" else hi, entry)
        if reached and worst <= 0:
            return True
        if best >= 0.5 * risk:
            reached = True
    return False


def _approached_target(trade, direction, target, tick) -> bool:
    if target is None:
        return False
    for step in trade.path:
        hi, lo = step["excursion_high"], step["excursion_low"]
        if hi is None or lo is None:
            continue
        reach = hi if direction == "LONG" else lo
        if direction == "LONG" and target - reach <= tick and reach < target:
            return True
        if direction == "SHORT" and reach - target <= tick and reach > target:
            return True
    return False


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def label_trade(m: dict, config: LabelConfig) -> dict:
    win = m["exit_reason"] == "TARGET"
    duration = m["duration_minutes"] or 0
    half_minutes = m["minutes_to_half_r"]

    clean = config.clean_win
    clean_conditions = {
        "clean_target_first": win,
        "clean_mae_ok": m["mae_r"] <= clean.max_mae_r,
        "clean_recross_ok": m["entry_crossings"] <= clean.max_entry_recross,
        "clean_half_r_fast": half_minutes is not None
        and half_minutes <= clean.reach_half_r_within_minutes,
        "clean_duration_ok": duration <= clean.max_duration_minutes,
    }
    is_clean = all(clean_conditions.values())

    sweaty = config.sweaty_win
    sweaty_conditions = {
        "sweaty_deep_mae": m["mae_r"] > sweaty.mae_r_above,
        "sweaty_entry_crossings": m["entry_crossings"] >= sweaty.entry_cross_at_least,
        "sweaty_midpoint_crossings": m["midpoint_crossings"] >= sweaty.midpoint_cross_at_least,
        "sweaty_slow_half_r": half_minutes is None
        or half_minutes > sweaty.fail_half_r_within_minutes,
        "sweaty_long_duration": duration > sweaty.duration_over_minutes,
        "sweaty_returned_near_stop": bool(m["returned_to_entry_after_half_r"]),
        "sweaty_high_overlap": m["post_entry_overlap"] > sweaty.post_entry_overlap_above,
    }
    sweaty_met = sum(sweaty_conditions.values())
    is_sweaty = win and sweaty_met >= sweaty.min_conditions

    is_immediate_failure = (
        m["exit_reason"] == "STOP" and m["mfe_r"] < config.immediate_failure.max_mfe_r
    )
    is_stalled = (
        m["exit_reason"] not in ("TARGET", "STOP")
        and duration >= config.stalled.after_minutes
        and m["mfe_r"] < config.stalled.mfe_r_below
    )

    rng = config.ranging
    net_progress_r = abs(m["result_r"])
    range_conditions = {
        "range_entry_crossings": m["entry_crossings"] >= rng.entry_cross_at_least,
        "range_midpoint_crossings": m["midpoint_crossings"] >= rng.midpoint_cross_at_least,
        "range_low_efficiency": m["efficiency_ratio_10"] < rng.efficiency_ratio_below,
        "range_low_progress": net_progress_r < rng.net_progress_r_below,
        "range_high_overlap": m["post_entry_overlap"] > rng.overlap_above,
        "range_no_resolution": m["bars_in_trade"] > rng.no_resolution_bars
        and m["exit_reason"] not in ("TARGET", "STOP"),
        "range_direction_flips": m.get("zone_inversions_during_trade", 0)
        >= rng.direction_changes_at_least,
    }
    range_met = sum(range_conditions.values())
    is_ranging = range_met >= rng.min_conditions

    if win:
        label = "CLEAN_WIN" if is_clean else ("SWEATY_WIN" if is_sweaty else "WIN")
    elif is_immediate_failure:
        label = "IMMEDIATE_FAILURE"
    elif m["exit_reason"] == "STOP":
        label = "LOSS"
    elif is_stalled:
        label = "STALLED"
    else:
        label = "TIMEOUT"

    return {
        "trade_label": label,
        "is_clean_win": is_clean and win,
        "is_sweaty_win": is_sweaty,
        "is_immediate_failure": is_immediate_failure,
        "is_stalled": is_stalled,
        "is_ranging": is_ranging,
        "sweaty_conditions_met": sweaty_met,
        "range_conditions_met": range_met,
        **clean_conditions,
        **sweaty_conditions,
        **range_conditions,
    }


# ---------------------------------------------------------------------------
# retrospective range onset (research label — never an entry feature)
# ---------------------------------------------------------------------------


def range_onset(
    bars: list[dict], start_index: int, config: RangeResearchConfig
) -> dict:
    """Earliest bar after formation whose *following* window is range-like.

    This looks into the future on purpose: it is a retrospective research
    label and must never be fed to a model as an entry-time feature.
    """
    window = config.onset_window_bars
    closes = np.array([b["close"] for b in bars], dtype=float)
    highs = np.array([b["high"] for b in bars], dtype=float)
    lows = np.array([b["low"] for b in bars], dtype=float)
    atrs = [b.get("atr") or 0.0 for b in bars]

    for i in range(start_index, len(bars) - window):
        seg = slice(i, i + window)
        eff = efficiency_ratio(closes[seg])
        ov = candle_overlap(highs[seg], lows[seg])
        net = abs(closes[i + window - 1] - closes[i])
        atr = atrs[i] or 1.0
        if (
            eff < config.onset_efficiency_below
            and ov > config.onset_overlap_above
            and net / atr < config.onset_net_progress_atr_below
        ):
            return {
                "range_onset_index": i,
                "range_onset_time": bars[i]["timestamp_ny"],
                "range_onset_minutes_after_formation": i - start_index,
                "range_onset_efficiency": eff,
                "range_onset_overlap": ov,
                "range_onset_net_atr": net / atr,
                "range_onset_found": True,
            }
    return {
        "range_onset_index": None,
        "range_onset_time": None,
        "range_onset_minutes_after_formation": None,
        "range_onset_efficiency": None,
        "range_onset_overlap": None,
        "range_onset_net_atr": None,
        "range_onset_found": False,
    }
