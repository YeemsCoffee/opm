"""First Presented FVG detection.

Scanning starts at ``fvg_search_start`` (09:30 New York).  By default all
three candles must begin at or after that time, so the earliest possible
completed FVG is 9:30 / 9:31 / 9:32 — configurable via
``fvg.all_candles_after_open`` (when False only candle 3 must complete after
the open).

Candidates are scanned chronologically; the first one satisfying Type A,
Type B, or both becomes the session's First Presented Significant FVG.
Candidates satisfying neither are rejected *with a reason* and scanning
continues.  Once selected, the zone is fixed for the session — later FVGs
never replace it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

import polars as pl

from ..config.schema import AppConfig
from .significance import TypeAResult, TypeBResult, evaluate_type_a, evaluate_type_b


class ZoneType(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class RejectionReason(StrEnum):
    NOT_SIGNIFICANT = "NOT_SIGNIFICANT"          # failed both Type A and Type B
    BELOW_MIN_TICK = "BELOW_MIN_TICK"            # gap narrower than one tick
    OUTSIDE_SEARCH_WINDOW = "OUTSIDE_SEARCH_WINDOW"
    ALREADY_SELECTED = "ALREADY_SELECTED"        # a zone was already chosen


@dataclass
class FvgCandidate:
    session_date: date
    direction: ZoneType
    c1_index: int
    c3_index: int
    c1_time: datetime
    c2_time: datetime
    c3_time: datetime
    fvg_low: float
    fvg_high: float
    gap_width: float
    proximal: float
    distal: float
    midpoint: float
    candle1_low: float
    candle1_high: float
    atr_at_formation: float
    type_a: TypeAResult
    type_b: TypeBResult
    selected: bool = False
    rejection_reason: RejectionReason | None = None
    displacement: dict = field(default_factory=dict)

    @property
    def significance_type(self) -> str | None:
        if self.type_a.passed and self.type_b.passed:
            return "A_AND_B"
        if self.type_a.passed:
            return "A_ONLY"
        if self.type_b.passed:
            return "B_ONLY"
        return None

    @property
    def is_significant(self) -> bool:
        return self.significance_type is not None

    def to_row(self) -> dict:
        row = {
            "session_date": self.session_date,
            "direction": str(self.direction),
            "c1_time": self.c1_time,
            "c2_time": self.c2_time,
            "c3_time": self.c3_time,
            "c1_index": self.c1_index,
            "c3_index": self.c3_index,
            "fvg_low": self.fvg_low,
            "fvg_high": self.fvg_high,
            "gap_width": self.gap_width,
            "proximal_edge": self.proximal,
            "distal_edge": self.distal,
            "midpoint": self.midpoint,
            "candle1_low": self.candle1_low,
            "candle1_high": self.candle1_high,
            "atr_at_formation": self.atr_at_formation,
            "significance_type": self.significance_type,
            "is_significant": self.is_significant,
            "selected": self.selected,
            "rejection_reason": str(self.rejection_reason) if self.rejection_reason else None,
        }
        row.update(self.type_a.to_dict())
        row.update(self.type_b.to_dict())
        row.update(self.displacement)
        return row


def _displacement_metrics(c2: dict, atr: float, tick: float) -> dict:
    rng = c2["high"] - c2["low"]
    body = abs(c2["close"] - c2["open"])
    close_loc = (c2["close"] - c2["low"]) / rng if rng > 0 else 0.5
    return {
        "displacement_range_points": rng,
        "displacement_range_atr": rng / atr if atr > 0 else 0.0,
        "displacement_body_points": body,
        "displacement_body_ratio": body / rng if rng > 0 else 0.0,
        "displacement_close_location": close_loc,
        "displacement_volume": c2.get("volume"),
        "displacement_range_ticks": rng / tick if tick else 0.0,
    }


def detect_candidates(
    bars: pl.DataFrame,
    config: AppConfig,
    session_date: date,
    cash_open: datetime,
    search_start: datetime,
    search_end: datetime,
    opening_range: float | None = None,
) -> list[FvgCandidate]:
    """Every 3-candle FVG in the search window, in chronological order.

    ``bars`` must be one session's one-minute candles (indicator columns
    attached) sorted ascending, including pre-open bars — Type B needs the
    candles before 9:30 and ATR needs the warm-up.
    """
    inst = config.active_instrument
    tick = inst.tick_size
    rows = bars.to_dicts()
    n = len(rows)
    out: list[FvgCandidate] = []
    strict = config.fvg.strict_inequality
    lookback = config.significance.type_b.prior_wick_lookback_minutes

    for i in range(2, n):
        c1, c2, c3 = rows[i - 2], rows[i - 1], rows[i]
        # window rule: candle 3 must complete inside the search window;
        # by default candle 1 (hence all three) must also start after it
        if not (search_start <= c3["timestamp_ny"] <= search_end):
            continue
        if config.fvg.all_candles_after_open and c1["timestamp_ny"] < search_start:
            continue

        gap_up = c3["low"] > c1["high"] if strict else c3["low"] >= c1["high"]
        gap_down = c3["high"] < c1["low"] if strict else c3["high"] <= c1["low"]
        if not (gap_up or gap_down):
            continue

        if gap_up:
            direction = ZoneType.BULLISH
            fvg_low, fvg_high = c1["high"], c3["low"]
            proximal, distal = fvg_high, fvg_low
        else:
            direction = ZoneType.BEARISH
            fvg_low, fvg_high = c3["high"], c1["low"]
            proximal, distal = fvg_low, fvg_high
        gap_width = fvg_high - fvg_low

        atr = c3.get("atr") or 0.0
        type_a = evaluate_type_a(
            direction=str(direction),
            gap_width=gap_width,
            atr=atr,
            c1_body_top=c1["body_top"],
            c1_body_bottom=c1["body_bottom"],
            c3_body_top=c3["body_top"],
            c3_body_bottom=c3["body_bottom"],
            candle2_range=c2["high"] - c2["low"],
            tick_size=tick,
            config=config.significance.type_a,
            opening_range=opening_range,
        )
        prior = [r for r in rows[max(0, i - 2 - lookback) : i - 2]]
        type_b = evaluate_type_b(
            prior_bars=prior,
            fvg_low=fvg_low,
            fvg_high=fvg_high,
            gap_width=gap_width,
            candle1_time=c1["timestamp_ny"],
            cash_open=cash_open,
            tick_size=tick,
            config=config.significance.type_b,
        )

        cand = FvgCandidate(
            session_date=session_date,
            direction=direction,
            c1_index=i - 2,
            c3_index=i,
            c1_time=c1["timestamp_ny"],
            c2_time=c2["timestamp_ny"],
            c3_time=c3["timestamp_ny"],
            fvg_low=fvg_low,
            fvg_high=fvg_high,
            gap_width=gap_width,
            proximal=proximal,
            distal=distal,
            midpoint=(fvg_low + fvg_high) / 2,
            candle1_low=c1["low"],
            candle1_high=c1["high"],
            atr_at_formation=atr,
            type_a=type_a,
            type_b=type_b,
            displacement=_displacement_metrics(c2, atr, tick),
        )
        if config.fvg.respect_min_tick and gap_width < tick:
            cand.rejection_reason = RejectionReason.BELOW_MIN_TICK
        out.append(cand)
    return out


def select_first_significant(
    candidates: list[FvgCandidate],
) -> tuple[FvgCandidate | None, list[FvgCandidate]]:
    """Mark and return the first significant candidate (plus all candidates).

    Rejected candidates keep a reason; candidates after the selection are
    marked ALREADY_SELECTED so the setups table shows the full scan.
    """
    selected: FvgCandidate | None = None
    for cand in candidates:
        if selected is not None:
            if cand.rejection_reason is None:
                cand.rejection_reason = RejectionReason.ALREADY_SELECTED
            continue
        if cand.rejection_reason is not None:  # e.g. BELOW_MIN_TICK
            continue
        if cand.is_significant:
            cand.selected = True
            selected = cand
        else:
            cand.rejection_reason = RejectionReason.NOT_SIGNIFICANT
    return selected, candidates
