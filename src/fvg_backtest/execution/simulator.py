"""Session-level trade simulation.

One session at a time, strictly forward in time:

1. every completed bar is pushed to the pivot tracker (confirming swing
   points without lookahead);
2. the first significant FVG after the search start fixes the session's
   reference zone;
3. an original order is armed for the *next* bar — candle 3 can never fill
   the order it created;
4. each later bar is resolved in this order: **fills and exits first**
   (they happen inside the bar), then the zone update (mitigation and
   inversion, which depend on the completed close);
5. an inversion cancels the opposing pending order and arms a new one from
   the following bar.

Only one position is open at a time; nothing is carried past the session
end, a contract change, or a data gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from ..config.schema import AppConfig
from ..fvg.detector import FvgCandidate, detect_candidates, select_first_significant
from ..fvg.zone import ZoneStateMachine
from ..liquidity.pivots import PivotSide, PivotTracker
from ..liquidity.targets import TargetSelection, select_target
from ..sessions.clock import SessionClock
from .costs import CostModel
from .intrabar import IntrabarResolver
from .orders import (
    CancelReason,
    Order,
    OrderState,
    build_inversion_order,
    build_original_order,
)

MAX_DATA_GAP_MINUTES = 2


@dataclass
class TradeRecord:
    """One filled trade and the bar-by-bar path it travelled."""

    order: Order
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None = None
    exit_price: float | None = None          # cost-adjusted fill
    gross_exit_price: float | None = None    # the level itself, before costs
    exit_reason: str | None = None
    path: list[dict] = field(default_factory=list)
    ambiguous_events: int = 0
    ambiguity_kinds: list[str] = field(default_factory=list)
    inversion_while_open: bool = False
    mitigations_before_entry: int = 0
    mitigations_after_entry: int = 0
    closes_inside_before_entry: int = 0
    had_data_gap: bool = False

    @property
    def is_open(self) -> bool:
        return self.exit_time is None


@dataclass
class SessionResult:
    session_date: date
    contract: str
    symbol: str
    candidates: list[FvgCandidate] = field(default_factory=list)
    selected: FvgCandidate | None = None
    zone: ZoneStateMachine | None = None
    orders: list[Order] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    target_selections: list[TargetSelection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    def log(self, kind: str, when: datetime, **detail) -> None:
        self.events.append(
            {
                "session_date": self.session_date,
                "underlying_contract": self.contract,
                "symbol": self.symbol,
                "event": kind,
                "timestamp": when,
                **detail,
            }
        )


class TradeSimulator:
    def __init__(self, config: AppConfig, clock: SessionClock) -> None:
        self.config = config
        self.clock = clock
        self.costs = CostModel(config.active_instrument)
        self.resolver = IntrabarResolver(config.execution.mode)

    # -- helpers ------------------------------------------------------------

    def _select_target(
        self, tracker: PivotTracker, direction: str, entry: float, now: datetime, atr: float
    ) -> TargetSelection:
        return select_target(
            tracker,
            direction=direction,
            entry=entry,
            now=now,
            liquidity_config=self.config.liquidity,
            target_config=self.config.targets,
            equal_levels=self.config.equal_levels,
            tick=self.costs.tick,
            atr=atr,
        )

    # -- main ---------------------------------------------------------------

    def run_session(
        self,
        bars: pl.DataFrame,
        session_date: date,
        *,
        finer: dict[datetime, list[dict]] | None = None,
    ) -> SessionResult:
        rows = bars.sort("timestamp_utc").to_dicts()
        contract = rows[0]["underlying_contract"] if rows else ""
        result = SessionResult(
            session_date=session_date,
            contract=contract,
            symbol=rows[0]["symbol"] if rows else self.config.instrument,
        )
        if not rows:
            result.skipped_reason = "NO_DATA"
            return result

        cash_open = self.clock.cash_open_dt(session_date)
        search_start = self.clock.fvg_search_start_dt(session_date)
        search_end = self.clock.fvg_search_end_dt(session_date)
        manage_end = self.clock.management_end_dt(session_date)

        opening_range = _opening_range(rows, cash_open, self.config.context)
        candidates = detect_candidates(
            bars, self.config, session_date, cash_open, search_start, search_end,
            opening_range=opening_range,
        )
        selected, candidates = select_first_significant(candidates)
        result.candidates = candidates
        for c in candidates:
            result.log(
                "CANDIDATE_DETECTED", c.c3_time,
                direction=str(c.direction), gap_width=c.gap_width,
                significance_type=c.significance_type,
            )
            if not c.selected:
                result.log(
                    "CANDIDATE_REJECTED", c.c3_time,
                    reason=str(c.rejection_reason) if c.rejection_reason else "NOT_SIGNIFICANT",
                )
        if selected is None:
            result.skipped_reason = "NO_SIGNIFICANT_FVG"
            return result

        result.selected = selected
        result.log(
            "FVG_SELECTED", selected.c3_time,
            direction=str(selected.direction), significance_type=selected.significance_type,
            fvg_low=selected.fvg_low, fvg_high=selected.fvg_high,
        )
        zone = ZoneStateMachine(fvg=selected, config=self.config.zone, tick_size=self.costs.tick)
        result.zone = zone

        tracker = PivotTracker(self.config.liquidity, tick_size=self.costs.tick)
        pending: Order | None = None
        open_trade: TradeRecord | None = None
        prev_ts: datetime | None = None
        inversion_orders = 0

        for i, bar in enumerate(rows):
            ts = bar["timestamp_ny"]
            tracker.push(bar)

            # bars up to and including candle 3 only build history
            if i <= selected.c3_index:
                if i == selected.c3_index:
                    pending = self._arm_original(zone, tracker, bar, rows, i, result)
                prev_ts = ts
                continue

            gap = (ts - prev_ts).total_seconds() / 60 if prev_ts else 1
            prev_ts = ts
            data_gap = gap > MAX_DATA_GAP_MINUTES
            contract_changed = bar["underlying_contract"] != contract

            if contract_changed:
                result.warnings.append(f"contract changed mid-session at {ts}")
                pending, open_trade = self._flush(
                    pending, open_trade, bar, result, CancelReason.CONTRACT_CHANGED,
                    "CONTRACT_ROLL",
                )
                break
            if data_gap:
                result.warnings.append(f"data gap of {gap:.0f} minutes before {ts}")
                if open_trade:
                    open_trade.had_data_gap = True
                if pending and pending.state == OrderState.PENDING:
                    pending.cancel(ts, CancelReason.DATA_INCOMPLETE)
                    result.log("ORDER_CANCELLED", ts, reason=str(CancelReason.DATA_INCOMPLETE))
                    pending = None

            past_management_end = ts > manage_end
            if past_management_end:
                pending, open_trade = self._flush(
                    pending, open_trade, bar, result, CancelReason.SESSION_END, "SESSION_CLOSE",
                )
                break

            # ---- 1. fills and exits (they happen inside the bar) ----------
            pending, open_trade = self._resolve_bar(
                bar, pending, open_trade, tracker, zone, result, finer,
            )

            # ---- 2. zone update from the completed close -------------------
            events = zone.update(bar)
            for ev in events:
                result.log(ev.kind, ev.timestamp, **{
                    "zone_state": str(ev.state), "price": ev.price, **ev.detail
                })
                if ev.kind == "MITIGATION":
                    if open_trade:
                        open_trade.mitigations_after_entry += 1
                    elif pending:
                        pending.context["mitigations_before_entry"] = (
                            pending.context.get("mitigations_before_entry", 0) + 1
                        )
                if ev.kind in ("INVERSION", "REINVERSION"):
                    if open_trade:
                        open_trade.inversion_while_open = True
                    if pending and pending.state == OrderState.PENDING:
                        pending.cancel(ts, CancelReason.ZONE_INVERTED_AGAINST)
                        result.log(
                            "ORDER_CANCELLED", ts,
                            reason=str(CancelReason.ZONE_INVERTED_AGAINST),
                        )
                        pending = None
                    if (
                        self.config.inversion.enabled
                        and open_trade is None
                        and inversion_orders < self.config.inversion.max_reinversion_entries
                        and i + 1 < len(rows)
                    ):
                        inversion_orders += 1
                        pending = self._arm_inversion(
                            zone, tracker, bar, rows, i, result, inversion_orders,
                        )

            # ---- 3. pending-order housekeeping -----------------------------
            if pending and pending.state == OrderState.PENDING:
                self._check_cancellations(pending, bar, tracker, result)
                if pending.state == OrderState.CANCELLED:
                    pending = None

        # session ran out of bars with something still live
        if rows:
            last = rows[-1]
            if open_trade and open_trade.is_open:
                self._close_trade(open_trade, last, last["close"], "SESSION_CLOSE", result)
            if pending and pending.state == OrderState.PENDING:
                pending.cancel(last["timestamp_ny"], CancelReason.SESSION_END)
                result.log("ORDER_CANCELLED", last["timestamp_ny"], reason="SESSION_END")
        return result

    # -- order arming -------------------------------------------------------

    def _arm_original(self, zone, tracker, bar, rows, i, result) -> Order | None:
        atr = zone.fvg.atr_at_formation
        direction = zone.state.trade_direction
        entry = _entry_for(zone, self.config.entries.model)
        sel = self._select_target(tracker, direction, entry, bar["timestamp_ny"], atr)
        result.target_selections.append(sel)
        if i + 1 >= len(rows):
            return None
        order = build_original_order(
            zone,
            entry_config=self.config.entries,
            target=sel.price,
            created_at=bar["timestamp_ny"],
            activated_at=rows[i + 1]["timestamp_ny"],
        )
        order.context["target_selection"] = sel
        result.orders.append(order)
        result.log(
            "ENTRY_ACTIVATION", order.activated_at,
            order_kind=order.kind, direction=order.direction,
            entry=order.entry, stop=order.stop, target=order.target,
        )
        if not sel.found:
            order.cancel(order.activated_at, CancelReason.NO_TARGET)
            result.log("ORDER_CANCELLED", order.activated_at, reason="NO_TARGET")
            return None
        return order

    def _arm_inversion(self, zone, tracker, bar, rows, i, result, index) -> Order | None:
        atr = zone.fvg.atr_at_formation
        direction = zone.state.trade_direction
        entry = _entry_for(zone, self.config.inversion.entry_model)
        sel = self._select_target(tracker, direction, entry, bar["timestamp_ny"], atr)
        result.target_selections.append(sel)
        swing = _recent_swing(tracker, direction, bar["timestamp_ny"])
        order = build_inversion_order(
            zone,
            config=self.config.inversion,
            target=sel.price,
            created_at=bar["timestamp_ny"],
            activated_at=rows[i + 1]["timestamp_ny"],
            tick=self.costs.tick,
            atr=atr,
            inversion_bar=bar,
            recent_swing=swing,
            inversion_index=index,
        )
        if order is None:
            result.warnings.append(
                f"{self.config.inversion.stop_model} produced no usable stop at {bar['timestamp_ny']}"
            )
            return None
        order.context["target_selection"] = sel
        result.orders.append(order)
        result.log(
            "ENTRY_ACTIVATION", order.activated_at,
            order_kind=order.kind, direction=order.direction,
            entry=order.entry, stop=order.stop, target=order.target,
        )
        if not sel.found:
            order.cancel(order.activated_at, CancelReason.NO_TARGET)
            result.log("ORDER_CANCELLED", order.activated_at, reason="NO_TARGET")
            return None
        return order

    # -- per-bar resolution --------------------------------------------------

    def _resolve_bar(self, bar, pending, open_trade, tracker, zone, result, finer):
        ts = bar["timestamp_ny"]
        active = (
            pending
            if pending and pending.state == OrderState.PENDING and pending.activated_at <= ts
            else None
        )
        if active is None and open_trade is None:
            return pending, open_trade

        order = open_trade.order if open_trade else active
        rows = finer.get(bar["timestamp_utc"]) if finer else None
        events = self.resolver.resolve(
            bar,
            direction=order.direction,
            entry=None if open_trade else order.entry,
            stop=order.stop,
            target=order.target,
            position_open=open_trade is not None,
            finer=rows,
        )
        if events.ambiguous:
            result.log("AMBIGUOUS_SEQUENCE", ts, ambiguity=str(events.ambiguity))

        for kind, price, when in events.sequence:
            if kind == "ENTRY":
                fill = self.costs.entry_fill(price, order.direction)
                order.fill(when, fill)
                open_trade = TradeRecord(
                    order=order,
                    entry_time=when,
                    entry_price=fill,
                    mitigations_before_entry=order.context.get("mitigations_before_entry", 0),
                    closes_inside_before_entry=zone.closes_inside,
                )
                pending = None
                result.log(
                    "ENTRY_FILL", when, price=fill, order_kind=order.kind,
                    direction=order.direction,
                )
            elif kind == "STOP" and open_trade:
                self._close_trade(open_trade, bar, price, "STOP", result, when=when)
            elif kind == "TARGET" and open_trade:
                self._close_trade(open_trade, bar, price, "TARGET", result, when=when)

        if open_trade:
            if events.ambiguous:
                open_trade.ambiguous_events += 1
                open_trade.ambiguity_kinds.append(str(events.ambiguity))
            open_trade.path.append(
                {
                    "timestamp": ts,
                    "open": bar["open"], "high": bar["high"],
                    "low": bar["low"], "close": bar["close"],
                    "excursion_low": events.excursion_low,
                    "excursion_high": events.excursion_high,
                    "atr": bar.get("atr"),
                }
            )
            if not open_trade.is_open:
                result.trades.append(open_trade)
                open_trade = None
        return pending, open_trade

    def _close_trade(self, trade, bar, price, reason, result, when=None):
        when = when or bar["timestamp_ny"]
        direction = trade.order.direction
        if reason == "STOP":
            fill = self.costs.stop_fill(price, direction)
        elif reason == "TARGET":
            fill = self.costs.target_fill(price, direction)
        else:
            fill = self.costs.market_exit_fill(price, direction)
        trade.exit_time = when
        trade.exit_price = fill
        trade.exit_reason = reason
        trade.gross_exit_price = price
        result.log("EXIT_" + reason, when, price=fill, order_kind=trade.order.kind)

    def _flush(self, pending, open_trade, bar, result, cancel_reason, event_kind):
        ts = bar["timestamp_ny"]
        result.log(event_kind, ts)
        if open_trade and open_trade.is_open:
            self._close_trade(open_trade, bar, bar["close"], "SESSION_CLOSE", result)
            result.trades.append(open_trade)
        if pending and pending.state == OrderState.PENDING:
            pending.cancel(ts, cancel_reason)
            result.log("ORDER_CANCELLED", ts, reason=str(cancel_reason))
        return None, None

    def _check_cancellations(self, order, bar, tracker, result) -> None:
        ts = bar["timestamp_ny"]
        if ts < order.activated_at:
            return
        age = order.age_minutes(ts)
        if age > self.config.orders.max_order_age_minutes:
            order.cancel(ts, CancelReason.MAX_AGE)
            result.log("ORDER_CANCELLED", ts, reason="MAX_AGE", age_minutes=age)
            return
        if self.config.orders.cancel_when_target_swept and order.target is not None:
            sel: TargetSelection | None = order.context.get("target_selection")
            if sel and sel.pivot is not None and sel.pivot.is_swept:
                order.cancel(ts, CancelReason.TARGET_SWEPT)
                result.log("ORDER_CANCELLED", ts, reason="TARGET_SWEPT")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _entry_for(zone: ZoneStateMachine, model: str) -> float:
    from .orders import entry_price_for

    return entry_price_for(zone, model)


def _recent_swing(tracker: PivotTracker, direction: str, now: datetime) -> float | None:
    side = PivotSide.LOW if direction == "LONG" else PivotSide.HIGH
    candidates = [p for p in tracker.pivots if p.side == side and p.confirmed_at <= now]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.timestamp).price


def _opening_range(rows: list[dict], cash_open: datetime, context_config) -> float | None:
    end = cash_open + timedelta(minutes=context_config.opening_range_minutes_long)
    window = [r for r in rows if cash_open <= r["timestamp_ny"] < end]
    if not window:
        return None
    return max(r["high"] for r in window) - min(r["low"] for r in window)
