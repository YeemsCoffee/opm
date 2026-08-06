"""Persistent zone state machine.

The selected FVG is a *reference zone* for the whole cash session:

- **Mitigation does not invalidate it.**  Price trading into the zone is
  recorded (touches, penetration, closes inside, time inside) and nothing
  more.
- **A wick completely through the zone does not invert it.**  Only a
  completed candle *close* beyond the opposite boundary flips direction.
- The same zone may invert repeatedly (inversion -> re-inversion -> …), and
  every flip is timestamped with the context needed for range research.

Direction states::

    ORIGINAL_BULLISH --close < fvg_low--> BEARISH_INVERSION
                     <--close > fvg_high-- BULLISH_REINVERSION
    ORIGINAL_BEARISH --close > fvg_high--> BULLISH_INVERSION
                     <--close < fvg_low-- BEARISH_REINVERSION
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ..config.schema import ZoneConfig
from .detector import FvgCandidate, ZoneType


class ZoneState(StrEnum):
    ORIGINAL_BULLISH = "ORIGINAL_BULLISH"
    ORIGINAL_BEARISH = "ORIGINAL_BEARISH"
    BEARISH_INVERSION = "BEARISH_INVERSION"
    BULLISH_INVERSION = "BULLISH_INVERSION"
    BULLISH_REINVERSION = "BULLISH_REINVERSION"
    BEARISH_REINVERSION = "BEARISH_REINVERSION"

    @property
    def is_bullish(self) -> bool:
        return self in (
            ZoneState.ORIGINAL_BULLISH,
            ZoneState.BULLISH_INVERSION,
            ZoneState.BULLISH_REINVERSION,
        )

    @property
    def trade_direction(self) -> str:
        return "LONG" if self.is_bullish else "SHORT"


@dataclass
class ZoneEvent:
    kind: str          # MITIGATION | INVERSION | REINVERSION | CLOSE_INSIDE | ...
    timestamp: datetime
    state: ZoneState
    price: float
    detail: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "event": self.kind,
            "timestamp": self.timestamp,
            "zone_state": str(self.state),
            "price": self.price,
            **{f"detail_{k}": v for k, v in self.detail.items()},
        }


@dataclass
class ZoneStateMachine:
    """Tracks one selected zone through the rest of the cash session."""

    fvg: FvgCandidate
    config: ZoneConfig
    tick_size: float = 0.25

    state: ZoneState = field(init=False)
    events: list[ZoneEvent] = field(default_factory=list)

    # mitigation stats
    first_touch_time: datetime | None = None
    touch_count: int = 0
    max_penetration: float = 0.0          # deepest travel into the zone, points
    max_penetration_ratio: float = 0.0    # as a fraction of gap width
    midpoint_touches: int = 0
    distal_touches: int = 0
    closes_inside: int = 0
    minutes_inside: int = 0
    wicks_through: int = 0

    # inversion stats
    inversion_count: int = 0
    last_inversion_time: datetime | None = None
    inversion_times: list[datetime] = field(default_factory=list)
    complete_crossings: int = 0
    midpoint_crossings: int = 0

    _prev_side: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.state = (
            ZoneState.ORIGINAL_BULLISH
            if self.fvg.direction == ZoneType.BULLISH
            else ZoneState.ORIGINAL_BEARISH
        )

    # -- properties -------------------------------------------------------

    @property
    def low(self) -> float:
        return self.fvg.fvg_low

    @property
    def high(self) -> float:
        return self.fvg.fvg_high

    @property
    def midpoint(self) -> float:
        return self.fvg.midpoint

    @property
    def gap_width(self) -> float:
        return self.fvg.gap_width

    def proximal_edge(self) -> float:
        """Entry-side edge for the *current* state.

        Original bullish: price returns down to ``fvg_high``.
        Bearish inversion of a bullish zone: price returns up to ``fvg_low``.
        """
        return self.high if self.state.is_bullish else self.low

    def distal_edge(self) -> float:
        return self.low if self.state.is_bullish else self.high

    # -- per-bar update ----------------------------------------------------

    def update(self, bar: dict) -> list[ZoneEvent]:
        """Feed one *completed* candle; returns the events it produced."""
        produced: list[ZoneEvent] = []
        ts = bar["timestamp_ny"]
        high, low, close = bar["high"], bar["low"], bar["close"]

        # --- mitigation (price trading into the zone) ---------------------
        touches = high >= self.low and low <= self.high
        if touches:
            self.touch_count += 1
            self.minutes_inside += 1
            if self.first_touch_time is None:
                self.first_touch_time = ts
            penetration = self._penetration(high, low)
            if penetration > self.max_penetration:
                self.max_penetration = penetration
                self.max_penetration_ratio = (
                    penetration / self.gap_width if self.gap_width > 0 else 0.0
                )
            if low <= self.midpoint <= high:
                self.midpoint_touches += 1
            if self._touches_distal(high, low):
                self.distal_touches += 1
            if self.low <= close <= self.high:
                self.closes_inside += 1
                produced.append(
                    ZoneEvent("CLOSE_INSIDE", ts, self.state, close,
                              {"penetration": penetration})
                )
            if low < self.low and high > self.high:
                # a wick straight through the whole zone — explicitly NOT an
                # inversion; only a close beyond the far edge can flip it
                self.wicks_through += 1
                produced.append(
                    ZoneEvent("WICK_THROUGH_ZONE", ts, self.state, close, {})
                )
            produced.append(
                ZoneEvent(
                    "MITIGATION", ts, self.state, close,
                    {
                        "touch_count": self.touch_count,
                        "penetration": penetration,
                        "penetration_ratio": (
                            penetration / self.gap_width if self.gap_width > 0 else 0.0
                        ),
                    },
                )
            )

        # --- midpoint crossings (close-based, direction agnostic) ---------
        side = 1 if close > self.midpoint else (-1 if close < self.midpoint else 0)
        if side != 0 and self._prev_side is not None and side != self._prev_side:
            self.midpoint_crossings += 1
        if side != 0:
            self._prev_side = side

        # --- inversion (completed close beyond the opposite boundary) -----
        event = self._check_inversion(close, ts)
        if event:
            produced.append(event)

        self.events.extend(produced)
        return produced

    # -- internals ----------------------------------------------------------

    def _penetration(self, high: float, low: float) -> float:
        """How deep price travelled into the zone from the proximal edge."""
        if self.state.is_bullish:
            deepest = max(self.low, min(low, self.high))
            return max(0.0, self.high - deepest)
        shallowest = min(self.high, max(high, self.low))
        return max(0.0, shallowest - self.low)

    def _touches_distal(self, high: float, low: float) -> bool:
        return low <= self.low if self.state.is_bullish else high >= self.high

    def _beyond(self, close: float, boundary: float, above: bool) -> bool:
        if above:
            return close > boundary or (self.config.invert_on_touch_close and close == boundary)
        return close < boundary or (self.config.invert_on_touch_close and close == boundary)

    def _check_inversion(self, close: float, ts: datetime) -> ZoneEvent | None:
        if self.state.is_bullish:
            if not self._beyond(close, self.low, above=False):
                return None
            new_state = (
                ZoneState.BEARISH_INVERSION
                if self.state == ZoneState.ORIGINAL_BULLISH
                else ZoneState.BEARISH_REINVERSION
            )
            distance = self.low - close
        else:
            if not self._beyond(close, self.high, above=True):
                return None
            new_state = (
                ZoneState.BULLISH_INVERSION
                if self.state == ZoneState.ORIGINAL_BEARISH
                else ZoneState.BULLISH_REINVERSION
            )
            distance = close - self.high

        prior_state = self.state
        since_formation = int((ts - self.fvg.c3_time).total_seconds() // 60)
        since_prev = (
            int((ts - self.last_inversion_time).total_seconds() // 60)
            if self.last_inversion_time
            else None
        )
        self.state = new_state
        self.inversion_count += 1
        self.complete_crossings += 1
        self.last_inversion_time = ts
        self.inversion_times.append(ts)
        atr = self.fvg.atr_at_formation
        kind = "REINVERSION" if "REINVERSION" in str(new_state) else "INVERSION"
        return ZoneEvent(
            kind, ts, new_state, close,
            {
                "from_state": str(prior_state),
                "inversion_index": self.inversion_count,
                "minutes_since_formation": since_formation,
                "minutes_since_previous_inversion": since_prev,
                "close_distance_beyond_zone": distance,
                "close_distance_beyond_zone_atr": distance / atr if atr > 0 else 0.0,
                "close_distance_beyond_zone_ticks": (
                    distance / self.tick_size if self.tick_size else 0.0
                ),
                "mitigations_before": self.touch_count,
                "closes_inside_before": self.closes_inside,
                "midpoint_crossings_before": self.midpoint_crossings,
                "complete_crossings_before": self.complete_crossings - 1,
            },
        )

    # -- summary ------------------------------------------------------------

    def summary(self) -> dict:
        return {
            "zone_state_final": str(self.state),
            "zone_first_touch_time": self.first_touch_time,
            "zone_touch_count": self.touch_count,
            "zone_max_penetration": self.max_penetration,
            "zone_max_penetration_ratio": self.max_penetration_ratio,
            "zone_midpoint_touches": self.midpoint_touches,
            "zone_distal_touches": self.distal_touches,
            "zone_closes_inside": self.closes_inside,
            "zone_minutes_inside": self.minutes_inside,
            "zone_wicks_through": self.wicks_through,
            "zone_inversion_count": self.inversion_count,
            "zone_midpoint_crossings": self.midpoint_crossings,
            "zone_complete_crossings": self.complete_crossings,
            "zone_last_inversion_time": self.last_inversion_time,
        }
