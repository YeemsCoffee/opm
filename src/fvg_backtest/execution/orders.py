"""Order construction and lifecycle.

**Original entries** use the zone's proximal edge with the stop at candle 1's
extreme::

    bullish: entry = fvg_high, stop = candle_1.low,  target = nearest untaken swing high
    bearish: entry = fvg_low,  stop = candle_1.high, target = nearest untaken swing low

**Inversion entries** flip to the opposite edge (a bearish inversion of a
bullish zone is entered at ``fvg_low``, approached from below) and take
their stop from the configured model.  Every inversion-stop model is
reported separately — results from different models are never pooled.

An order becomes active only *after* the confirming candle closes: candle 3
cannot both establish the FVG and fill the order, and an inversion cannot
fill on the candle that confirmed it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ..config.schema import BufferConfig, EntryConfig, InversionConfig
from ..fvg.zone import ZoneState, ZoneStateMachine


class OrderState(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class CancelReason(StrEnum):
    TARGET_SWEPT = "TARGET_SWEPT"
    ZONE_INVERTED_AGAINST = "ZONE_INVERTED_AGAINST"
    SESSION_END = "SESSION_END"
    MAX_AGE = "MAX_AGE"
    CONTRACT_CHANGED = "CONTRACT_CHANGED"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    UNSAFE_SEQUENCING = "UNSAFE_SEQUENCING"
    NO_TARGET = "NO_TARGET"


ORDER_AGE_BUCKETS = (
    (0, 1, "0-1 minute"),
    (2, 3, "2-3 minutes"),
    (4, 5, "4-5 minutes"),
    (6, 10, "6-10 minutes"),
    (11, 15, "11-15 minutes"),
    (16, 30, "16-30 minutes"),
    (31, 10**9, "31+ minutes"),
)


def order_age_bucket(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    for lo, hi, label in ORDER_AGE_BUCKETS:
        if lo <= minutes <= hi:
            return label
    return ORDER_AGE_BUCKETS[-1][2]


def entry_price_for(zone: ZoneStateMachine, model: str) -> float:
    """Entry level for the zone's current direction under an entry model."""
    if model == "MIDPOINT":
        return zone.midpoint
    if model == "DISTAL_EDGE":
        return zone.distal_edge()
    return zone.proximal_edge()


def buffer_points(buf: BufferConfig, tick: float, atr: float) -> float:
    if buf.unit == "points":
        return buf.points
    if buf.unit == "atr":
        return buf.atr * atr
    return buf.ticks * tick


def stop_price_for(
    zone: ZoneStateMachine,
    *,
    model: str,
    direction: str,
    buffer: BufferConfig,
    tick: float,
    atr: float,
    inversion_bar: dict | None = None,
    recent_swing: float | None = None,
) -> float | None:
    """Stop level for an inversion entry under the selected model."""
    pad = buffer_points(buffer, tick, atr)
    long = direction == "LONG"
    if model == "OPPOSITE_FVG_EDGE_PLUS_BUFFER":
        edge = zone.distal_edge()
        return edge - pad if long else edge + pad
    if model == "INVERSION_CANDLE_EXTREME_PLUS_BUFFER":
        if not inversion_bar:
            return None
        return (
            inversion_bar["low"] - pad if long else inversion_bar["high"] + pad
        )
    if model == "MOST_RECENT_SWING_PLUS_BUFFER":
        if recent_swing is None:
            return None
        return recent_swing - pad if long else recent_swing + pad
    if model == "ORIGINAL_CANDLE_1_EXTREME":
        return zone.fvg.candle1_low if long else zone.fvg.candle1_high
    raise ValueError(f"unknown inversion stop model {model!r}")


@dataclass
class Order:
    """One working order: original or inversion."""

    kind: str                      # ORIGINAL | INVERSION | REINVERSION
    direction: str                 # LONG | SHORT
    entry: float
    stop: float
    target: float | None
    zone_state: ZoneState
    activated_at: datetime         # first bar on which it may fill
    created_at: datetime           # the bar that produced it (cannot fill)
    entry_model: str
    stop_model: str
    inversion_index: int = 0
    state: OrderState = OrderState.PENDING
    filled_at: datetime | None = None
    fill_price: float | None = None
    cancelled_at: datetime | None = None
    cancel_reason: CancelReason | None = None
    context: dict = field(default_factory=dict)

    @property
    def risk_points(self) -> float:
        return abs(self.entry - self.stop)

    def risk_ticks(self, tick: float) -> float:
        return self.risk_points / tick if tick else 0.0

    def target_r(self) -> float | None:
        if self.target is None or self.risk_points <= 0:
            return None
        return abs(self.target - self.entry) / self.risk_points

    def age_minutes(self, now: datetime) -> int:
        return int((now - self.activated_at).total_seconds() // 60)

    def cancel(self, when: datetime, reason: CancelReason) -> None:
        self.state = OrderState.CANCELLED
        self.cancelled_at = when
        self.cancel_reason = reason

    def fill(self, when: datetime, price: float) -> None:
        self.state = OrderState.FILLED
        self.filled_at = when
        self.fill_price = price

    def to_dict(self, tick: float) -> dict:
        return {
            "order_kind": self.kind,
            "direction": self.direction,
            "entry_price": self.entry,
            "stop_price": self.stop,
            "target_price": self.target,
            "risk_points": self.risk_points,
            "risk_ticks": self.risk_ticks(tick),
            "target_r": self.target_r(),
            "entry_model": self.entry_model,
            "inversion_stop_model": self.stop_model,
            "zone_state_at_order": str(self.zone_state),
            "inversion_index": self.inversion_index,
            "order_created_at": self.created_at,
            "order_activated_at": self.activated_at,
            "order_state": str(self.state),
            "filled_at": self.filled_at,
            "fill_price": self.fill_price,
            "cancelled_at": self.cancelled_at,
            "cancel_reason": str(self.cancel_reason) if self.cancel_reason else None,
            "order_age_at_fill_minutes": (
                self.age_minutes(self.filled_at) if self.filled_at else None
            ),
            "order_age_bucket": order_age_bucket(
                self.age_minutes(self.filled_at) if self.filled_at else None
            ),
        }


def build_original_order(
    zone: ZoneStateMachine,
    *,
    entry_config: EntryConfig,
    target: float | None,
    created_at: datetime,
    activated_at: datetime,
) -> Order:
    direction = zone.state.trade_direction
    entry = entry_price_for(zone, entry_config.model)
    stop = zone.fvg.candle1_low if direction == "LONG" else zone.fvg.candle1_high
    return Order(
        kind="ORIGINAL",
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        zone_state=zone.state,
        created_at=created_at,
        activated_at=activated_at,
        entry_model=entry_config.model,
        stop_model="ORIGINAL_CANDLE_1_EXTREME",
    )


def build_inversion_order(
    zone: ZoneStateMachine,
    *,
    config: InversionConfig,
    target: float | None,
    created_at: datetime,
    activated_at: datetime,
    tick: float,
    atr: float,
    inversion_bar: dict | None = None,
    recent_swing: float | None = None,
    inversion_index: int = 1,
) -> Order | None:
    direction = zone.state.trade_direction
    entry = entry_price_for(zone, config.entry_model)
    stop = stop_price_for(
        zone,
        model=config.stop_model,
        direction=direction,
        buffer=config.stop_buffer,
        tick=tick,
        atr=atr,
        inversion_bar=inversion_bar,
        recent_swing=recent_swing,
    )
    if stop is None or (direction == "LONG" and stop >= entry) or (
        direction == "SHORT" and stop <= entry
    ):
        return None  # the model produced no usable stop for this side
    kind = "REINVERSION" if "REINVERSION" in str(zone.state) else "INVERSION"
    return Order(
        kind=kind,
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        zone_state=zone.state,
        created_at=created_at,
        activated_at=activated_at,
        entry_model=config.entry_model,
        stop_model=config.stop_model,
        inversion_index=inversion_index,
    )
