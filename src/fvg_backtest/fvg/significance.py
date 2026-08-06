"""Significance tests for a candidate FVG.

**Type A** — the gap is big enough *and* was not almost closed by the wicks
of candles 1 and 3::

    normalized_gap      = gap_width / atr
    body_void           = c3.body_bottom - c1.body_top     (bullish)
                          c1.body_bottom - c3.body_top     (bearish)
    preservation_ratio  = gap_width / body_void

``body_void`` is the distance between the two bodies; the gap can never
exceed it, so the ratio lives in (0, 1].  A ratio near 1 means the wicks
barely intruded; near 0 means the wicks nearly closed the gap.  Degenerate
body voids (<= 0, which happens when bodies overlap) yield a ratio of 0.0
and are reported, never crashed on.

**Type B** — at confirmation, at least one of the N completed candles before
candle 1 has a wick that is long (absolute + relative) *and* overlaps the
gap.  Every raw measurement is stored, not just the boolean.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..config.schema import TypeAConfig, TypeBConfig


@dataclass
class TypeAResult:
    passed: bool
    gap_width: float
    atr: float
    normalized_gap: float          # gap_width / atr
    body_void: float
    preservation_ratio: float      # gap_width / body_void, 0.0 when degenerate
    body_void_degenerate: bool
    gap_ticks: float
    gap_pct_candle2_range: float
    gap_pct_opening_range: float | None
    min_gap_atr: float
    min_preservation_ratio: float

    def to_dict(self) -> dict:
        return {f"type_a_{k}": v for k, v in asdict(self).items()}


@dataclass
class PriorWick:
    timestamp: datetime
    minutes_before_candle1: int
    side: str                 # UPPER | LOWER
    candle_direction: str     # BULLISH | BEARISH | DOJI
    wick_points: float
    wick_ticks: float
    wick_atr_ratio: float
    wick_share: float
    overlap_points: float
    overlap_ticks: float
    fvg_overlap_ratio: float
    after_cash_open: bool
    qualifies: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp
        return d


@dataclass
class TypeBResult:
    passed: bool
    qualifying_count: int
    examined_count: int
    wicks: list[PriorWick] = field(default_factory=list)
    closest: PriorWick | None = None
    largest: PriorWick | None = None
    any_qualifying_before_open: bool = False
    any_qualifying_after_open: bool = False

    def to_dict(self) -> dict:
        return {
            "type_b_passed": self.passed,
            "type_b_qualifying_count": self.qualifying_count,
            "type_b_examined_count": self.examined_count,
            "type_b_closest_wick_age_min": self.closest.minutes_before_candle1 if self.closest else None,
            "type_b_closest_wick_points": self.closest.wick_points if self.closest else None,
            "type_b_closest_wick_side": self.closest.side if self.closest else None,
            "type_b_closest_wick_timestamp": self.closest.timestamp if self.closest else None,
            "type_b_closest_wick_overlap_ratio": self.closest.fvg_overlap_ratio if self.closest else None,
            "type_b_closest_wick_share": self.closest.wick_share if self.closest else None,
            "type_b_closest_wick_atr_ratio": self.closest.wick_atr_ratio if self.closest else None,
            "type_b_largest_wick_points": self.largest.wick_points if self.largest else None,
            "type_b_largest_wick_age_min": self.largest.minutes_before_candle1 if self.largest else None,
            "type_b_largest_wick_overlap_ratio": self.largest.fvg_overlap_ratio if self.largest else None,
            "type_b_qualifying_before_open": self.any_qualifying_before_open,
            "type_b_qualifying_after_open": self.any_qualifying_after_open,
        }


def evaluate_type_a(
    *,
    direction: str,          # BULLISH | BEARISH
    gap_width: float,
    atr: float,
    c1_body_top: float,
    c1_body_bottom: float,
    c3_body_top: float,
    c3_body_bottom: float,
    candle2_range: float,
    tick_size: float,
    config: TypeAConfig,
    opening_range: float | None = None,
) -> TypeAResult:
    if direction == "BULLISH":
        body_void = c3_body_bottom - c1_body_top
    else:
        body_void = c1_body_bottom - c3_body_top

    degenerate = body_void <= 0
    preservation = 0.0 if degenerate else min(gap_width / body_void, 1.0)
    normalized = gap_width / atr if atr > 0 else 0.0
    passed = (
        normalized >= config.minimum_gap_atr
        and preservation >= config.minimum_preservation_ratio
    )
    return TypeAResult(
        passed=passed,
        gap_width=gap_width,
        atr=atr,
        normalized_gap=normalized,
        body_void=body_void,
        preservation_ratio=preservation,
        body_void_degenerate=degenerate,
        gap_ticks=gap_width / tick_size if tick_size else 0.0,
        gap_pct_candle2_range=gap_width / candle2_range if candle2_range > 0 else 0.0,
        gap_pct_opening_range=(
            gap_width / opening_range if opening_range and opening_range > 0 else None
        ),
        min_gap_atr=config.minimum_gap_atr,
        min_preservation_ratio=config.minimum_preservation_ratio,
    )


def evaluate_type_b(
    *,
    prior_bars: list[dict],   # oldest -> newest, each with OHLC + atr + timestamp_ny
    fvg_low: float,
    fvg_high: float,
    gap_width: float,
    candle1_time: datetime,
    cash_open: datetime,
    tick_size: float,
    config: TypeBConfig,
) -> TypeBResult:
    wicks: list[PriorWick] = []
    for bar in prior_bars:
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        atr = bar.get("atr") or 0.0
        body_top, body_bottom = max(o, c), min(o, c)
        rng = h - l
        direction = "BULLISH" if c > o else ("BEARISH" if c < o else "DOJI")
        minutes = int((candle1_time - bar["timestamp_ny"]).total_seconds() // 60)
        for side, wick_low, wick_high in (
            ("UPPER", body_top, h),
            ("LOWER", l, body_bottom),
        ):
            length = wick_high - wick_low
            if length <= 0:
                continue
            overlap = max(0.0, min(wick_high, fvg_high) - max(wick_low, fvg_low))
            share = length / rng if rng > 0 else 0.0
            atr_ratio = length / atr if atr > 0 else 0.0
            ratio = overlap / gap_width if gap_width > 0 else 0.0
            qualifies = (
                atr_ratio >= config.minimum_wick_atr
                and share >= config.minimum_wick_share
                and ratio >= config.minimum_fvg_overlap_ratio
            )
            wicks.append(
                PriorWick(
                    timestamp=bar["timestamp_ny"],
                    minutes_before_candle1=minutes,
                    side=side,
                    candle_direction=direction,
                    wick_points=length,
                    wick_ticks=length / tick_size if tick_size else 0.0,
                    wick_atr_ratio=atr_ratio,
                    wick_share=share,
                    overlap_points=overlap,
                    overlap_ticks=overlap / tick_size if tick_size else 0.0,
                    fvg_overlap_ratio=ratio,
                    after_cash_open=bar["timestamp_ny"] >= cash_open,
                    qualifies=qualifies,
                )
            )

    qualifying = [w for w in wicks if w.qualifies]
    closest = min(qualifying, key=lambda w: w.minutes_before_candle1, default=None)
    largest = max(qualifying, key=lambda w: w.wick_points, default=None)
    return TypeBResult(
        passed=bool(qualifying),
        qualifying_count=len(qualifying),
        examined_count=len(prior_bars),
        wicks=wicks,
        closest=closest,
        largest=largest,
        any_qualifying_before_open=any(not w.after_cash_open for w in qualifying),
        any_qualifying_after_open=any(w.after_cash_open for w in qualifying),
    )
