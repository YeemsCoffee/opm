"""Swing-point detection and sweep tracking, free of lookahead.

A pivot at bar ``i`` with strength ``k`` requires ``k`` bars on each side::

    swing high (1x1): high[i] > high[i-1] and high[i] >= high[i+1]
    swing low  (1x1): low[i]  < low[i-1]  and low[i]  <= low[i+1]

Strict on the left, non-strict on the right, so a flat right shoulder still
confirms.  For ``k >= 2`` the same asymmetry applies to every bar in the
window.  A pivot becomes **available** only once bar ``i + k`` has closed —
``PivotTracker.push`` therefore returns the pivots confirmed *by* the bar it
was just given, never pivots that need future bars.

Sweep state advances as later bars trade through the level:

``UNTOUCHED -> TOUCHED_ONCE -> TOUCHED_MULTIPLE_TIMES``, and ``SWEPT`` once
price trades beyond it by more than the configured tolerance.  An exact
touch is recorded separately and does not count as a sweep by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ..config.schema import LiquidityConfig


class PivotSide(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


class PivotStatus(StrEnum):
    UNTOUCHED = "UNTOUCHED"
    TOUCHED_ONCE = "TOUCHED_ONCE"
    TOUCHED_MULTIPLE_TIMES = "TOUCHED_MULTIPLE_TIMES"
    SWEPT = "SWEPT"


@dataclass
class Pivot:
    side: PivotSide
    price: float
    bar_index: int
    timestamp: datetime          # the pivot bar's time
    confirmed_at: datetime       # when it became usable (bar_index + strength)
    strength: int
    session_segment: str         # OVERNIGHT | PREMARKET | CASH | POST_CASH
    status: PivotStatus = PivotStatus.UNTOUCHED
    touch_count: int = 0
    exact_touch_count: int = 0
    swept_at: datetime | None = None

    @property
    def is_swept(self) -> bool:
        return self.status == PivotStatus.SWEPT

    @property
    def eligible(self) -> bool:
        """Untouched and touched-but-not-swept levels remain eligible."""
        return not self.is_swept

    def age_minutes(self, now: datetime) -> int:
        return int((now - self.timestamp).total_seconds() // 60)

    def to_dict(self) -> dict:
        return {
            "side": str(self.side),
            "price": self.price,
            "timestamp": self.timestamp,
            "confirmed_at": self.confirmed_at,
            "strength": self.strength,
            "status": str(self.status),
            "touch_count": self.touch_count,
            "exact_touch_count": self.exact_touch_count,
            "session_segment": self.session_segment,
            "swept_at": self.swept_at,
        }


@dataclass
class PivotTracker:
    """Streaming pivot detection + sweep bookkeeping for one session."""

    config: LiquidityConfig
    tick_size: float = 0.25

    bars: list[dict] = field(default_factory=list)
    pivots: list[Pivot] = field(default_factory=list)

    @property
    def strength(self) -> int:
        return int(self.config.pivot_strength)

    @property
    def _tolerance(self) -> float:
        return self.config.sweep_tolerance_ticks * self.tick_size

    def push(self, bar: dict) -> list[Pivot]:
        """Feed one completed bar; return pivots this bar confirms."""
        self.bars.append(bar)
        self._update_sweeps(bar)
        k = self.strength
        i = len(self.bars) - 1 - k  # candidate centre
        if i < k:
            return []
        confirmed = []
        for side in (PivotSide.HIGH, PivotSide.LOW):
            if self._is_pivot(i, side):
                p = Pivot(
                    side=side,
                    price=self.bars[i]["high"] if side == PivotSide.HIGH else self.bars[i]["low"],
                    bar_index=i,
                    timestamp=self.bars[i]["timestamp_ny"],
                    confirmed_at=bar["timestamp_ny"],
                    strength=k,
                    session_segment=self.bars[i].get("session_segment", "CASH"),
                )
                # bars between the pivot and now may already have swept it
                for later in self.bars[i + 1 :]:
                    self._apply_bar_to_pivot(p, later)
                self.pivots.append(p)
                confirmed.append(p)
        return confirmed

    def _is_pivot(self, i: int, side: PivotSide) -> bool:
        k = self.strength
        if i - k < 0 or i + k >= len(self.bars):
            return False
        if side == PivotSide.HIGH:
            centre = self.bars[i]["high"]
            left = all(centre > self.bars[i - j]["high"] for j in range(1, k + 1))
            right = all(centre >= self.bars[i + j]["high"] for j in range(1, k + 1))
        else:
            centre = self.bars[i]["low"]
            left = all(centre < self.bars[i - j]["low"] for j in range(1, k + 1))
            right = all(centre <= self.bars[i + j]["low"] for j in range(1, k + 1))
        return left and right

    def _update_sweeps(self, bar: dict) -> None:
        for p in self.pivots:
            if p.bar_index < len(self.bars) - 1:
                self._apply_bar_to_pivot(p, bar)

    def _apply_bar_to_pivot(self, p: Pivot, bar: dict) -> None:
        if p.is_swept:
            return
        tol = self._tolerance
        if p.side == PivotSide.HIGH:
            beyond = bar["high"] > p.price + tol
            exact = bar["high"] == p.price
            touched = bar["high"] >= p.price
        else:
            beyond = bar["low"] < p.price - tol
            exact = bar["low"] == p.price
            touched = bar["low"] <= p.price
        if exact:
            p.exact_touch_count += 1
            # an exact touch only sweeps when the config says so
            beyond = self.config.count_exact_touch_as_sweep
        if beyond:
            p.status = PivotStatus.SWEPT
            p.swept_at = bar["timestamp_ny"]
            p.touch_count += 1
            return
        if touched:
            p.touch_count += 1
            p.status = (
                PivotStatus.TOUCHED_ONCE
                if p.touch_count == 1
                else PivotStatus.TOUCHED_MULTIPLE_TIMES
            )

    # -- queries ---------------------------------------------------------

    def eligible_pivots(
        self,
        side: PivotSide,
        now: datetime,
        *,
        lookback_minutes: int,
        min_age_minutes: int = 0,
        allow_touched: bool = True,
    ) -> list[Pivot]:
        """Confirmed, unswept pivots inside the rolling lookback window."""
        out = []
        for p in self.pivots:
            if p.side != side or not p.eligible or p.confirmed_at > now:
                continue
            age = p.age_minutes(now)
            if age < min_age_minutes or age > lookback_minutes:
                continue
            if not allow_touched and p.touch_count > 0:
                continue
            out.append(p)
        return out
