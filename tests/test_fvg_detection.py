from __future__ import annotations

from datetime import date

import pytest

from fvg_backtest.config import AppConfig
from fvg_backtest.fvg import (
    RejectionReason,
    ZoneType,
    detect_candidates,
    select_first_significant,
)
from helpers import bars_from, body_bars, flat_bars

SESSION = date(2025, 1, 6)
B = 21000.0

# a quiet lead-in whose range [B+0.75, B+4.25] overlaps the FVG's candle 1
# and candle 2, so the only gap in these frames is the intended one
LEAD_IN = lambda n: flat_bars(n, price=B + 2.5, rng=1.75)
BULL_FVG = [
    (B + 2, B + 5, B + 1, B + 4),          # candle 1: high B+5  (low B+1 = stop)
    (B + 4, B + 13, B + 3.75, B + 12),     # candle 2: displacement
    (B + 10.5, B + 15, B + 9, B + 11.5),   # candle 3: low B+9
]


def _detect(clock, config, ohlc, start="09:15", opening_range=None):
    bars = bars_from(clock, SESSION, start, ohlc)
    return detect_candidates(
        bars,
        config,
        SESSION,
        cash_open=clock.cash_open_dt(SESSION),
        search_start=clock.fvg_search_start_dt(SESSION),
        search_end=clock.fvg_search_end_dt(SESSION),
        opening_range=opening_range,
    )


def test_bullish_fvg_boundaries(clock, config: AppConfig):
    # 16 quiet bars 9:15-9:30, then the FVG at 9:31 / 9:32 / 9:33
    cands = _detect(clock, config, LEAD_IN(16) + BULL_FVG)
    assert len(cands) == 1
    c = cands[0]
    assert c.direction == ZoneType.BULLISH
    assert c.fvg_low == B + 5      # candle_1.high
    assert c.fvg_high == B + 9     # candle_3.low
    assert c.gap_width == 4.0
    assert c.proximal == B + 9     # bullish proximal = fvg_high
    assert c.distal == B + 5       # bullish distal   = fvg_low
    assert c.midpoint == B + 7
    assert c.candle1_low == B + 1  # the original stop level
    assert c.c1_time.strftime("%H:%M") == "09:31"
    assert c.c3_time.strftime("%H:%M") == "09:33"


def test_bearish_fvg_boundaries(clock, config: AppConfig):
    ohlc = flat_bars(16, price=B - 2.5, rng=1.75) + [
        (B - 2, B - 1, B - 5, B - 4),          # candle 1: low B-5
        (B - 4, B - 3.75, B - 13, B - 12),     # candle 2
        (B - 10.5, B - 9, B - 15, B - 11.5),   # candle 3: high B-9
    ]
    cands = _detect(clock, config, ohlc)
    assert len(cands) == 1
    c = cands[0]
    assert c.direction == ZoneType.BEARISH
    assert c.fvg_low == B - 9      # candle_3.high
    assert c.fvg_high == B - 5     # candle_1.low
    assert c.gap_width == 4.0
    assert c.proximal == B - 9     # bearish proximal = fvg_low
    assert c.distal == B - 5       # bearish distal   = fvg_high
    assert c.candle1_high == B - 1


def test_touching_candles_are_not_a_gap(clock, config: AppConfig):
    # candle_3.low exactly equals candle_1.high -> no gap under strict rules
    ohlc = LEAD_IN(16) + [
        (B + 2, B + 5, B + 1, B + 4),
        (B + 4, B + 13, B + 3.75, B + 12),
        (B + 10.5, B + 15, B + 5, B + 11.5),
    ]
    assert _detect(clock, config, ohlc) == []
    loose = config.model_copy(deep=True)
    loose.fvg.strict_inequality = False
    assert len(_detect(clock, loose, ohlc)) == 1


def test_all_three_candles_must_be_after_open_by_default(clock, config: AppConfig):
    # gap formed by 9:28 / 9:29 / 9:30 -> candle 1 precedes the open
    ohlc = LEAD_IN(13) + BULL_FVG + flat_bars(3, price=B + 11)
    assert _detect(clock, config, ohlc) == []

    relaxed = config.model_copy(deep=True)
    relaxed.fvg.all_candles_after_open = False
    got = _detect(clock, relaxed, ohlc)
    assert len(got) == 1
    assert got[0].c3_time.strftime("%H:%M") == "09:30"


def test_earliest_possible_fvg_is_930_931_932(clock, config: AppConfig):
    cands = _detect(clock, config, LEAD_IN(15) + BULL_FVG)
    assert len(cands) == 1
    assert cands[0].c1_time.strftime("%H:%M") == "09:30"
    assert cands[0].c3_time.strftime("%H:%M") == "09:32"


def test_first_significant_wins_and_later_ones_are_marked(clock, config: AppConfig):
    second = [
        (B + 13, B + 15, B + 12, B + 14),
        (B + 14, B + 23, B + 13.75, B + 22),
        (B + 20.5, B + 25, B + 19, B + 21.5),
    ]
    ohlc = LEAD_IN(16) + BULL_FVG + flat_bars(4, price=B + 13, rng=2) + second
    cands = _detect(clock, config, ohlc)
    assert len(cands) == 2
    selected, all_cands = select_first_significant(cands)
    assert selected is not None
    assert selected.c3_time.strftime("%H:%M") == "09:33"
    later = [c for c in all_cands if not c.selected]
    assert later and all(
        c.rejection_reason == RejectionReason.ALREADY_SELECTED for c in later
    )


# a 0.25-point gap with a 2.0-point body void: preservation 0.125 fails Type A,
# and the negligible-wick lead-in fails Type B
TINY_FVG = [
    (B - 0.5, B + 0.5, B - 0.6, B + 0.4),
    (B + 0.4, B + 2.0, B + 0.35, B + 1.9),
    (B + 2.4, B + 2.6, B + 0.75, B + 2.5),
]


def test_insignificant_candidate_is_rejected_and_scanning_continues(clock, config: AppConfig):
    ohlc = (
        body_bars(16, price=B)
        + TINY_FVG
        + body_bars(3, price=B + 2.5, body=2.5)
        + BULL_FVG
    )
    cands = _detect(clock, config, ohlc)
    assert len(cands) == 2
    selected, all_cands = select_first_significant(cands)
    assert selected.c3_time.strftime("%H:%M") == "09:39"

    first = all_cands[0]
    assert first.rejection_reason == RejectionReason.NOT_SIGNIFICANT
    assert not first.is_significant
    assert first.type_a.preservation_ratio == pytest.approx(0.125)
    assert not first.type_b.passed


# 9:25 bar whose 3.75-point upper wick reaches B+7, half-way into the
# future zone [B+5, B+9]: overlap ratio 0.50, wick share 0.88
WICK_BAR = (B + 3, B + 7, B + 2.75, B + 3.25)
WICK_LEAD_IN = LEAD_IN(10) + [WICK_BAR] + LEAD_IN(5)


def test_significance_classification_labels(clock, config: AppConfig):
    # the plain lead-in never reaches the zone -> size alone qualifies it
    c = _detect(clock, config, LEAD_IN(16) + BULL_FVG)[0]
    assert c.type_a.passed and not c.type_b.passed
    assert c.significance_type == "A_ONLY"

    # add a prior wick reaching into the zone -> both tests pass
    c = _detect(clock, config, WICK_LEAD_IN + BULL_FVG)[0]
    assert c.significance_type == "A_AND_B"
    assert c.type_b.qualifying_count == 1
    assert c.type_b.closest.minutes_before_candle1 == 6      # 9:25 -> 9:31
    assert c.type_b.closest.side == "UPPER"
    assert c.type_b.closest.fvg_overlap_ratio == pytest.approx(0.5)

    # raise the Type A bar beyond reach -> B_ONLY, still significant
    b_only = config.model_copy(deep=True)
    b_only.significance.type_a.minimum_gap_atr = 99.0
    c = _detect(clock, b_only, WICK_LEAD_IN + BULL_FVG)[0]
    assert c.significance_type == "B_ONLY"
    assert c.is_significant

    # …and the Type B bar as well -> not significant at all
    neither = b_only.model_copy(deep=True)
    neither.significance.type_b.minimum_fvg_overlap_ratio = 99.0
    c = _detect(clock, neither, WICK_LEAD_IN + BULL_FVG)[0]
    assert c.significance_type is None
    assert not c.is_significant


def test_rejected_candidates_keep_every_raw_measurement(clock, config: AppConfig):
    cands = _detect(clock, config, body_bars(16, price=B) + TINY_FVG)
    row = cands[0].to_row()
    # the raw numbers survive even though the candidate failed
    assert row["gap_width"] == pytest.approx(0.25)
    assert row["type_a_normalized_gap"] > 0
    assert row["type_a_preservation_ratio"] == pytest.approx(0.125)
    assert row["type_a_body_void"] == pytest.approx(2.0)
    assert row["type_b_examined_count"] == 15
    assert row["significance_type"] is None
    assert row["displacement_body_ratio"] > 0
    assert row["type_a_gap_ticks"] == pytest.approx(1.0)


def test_gap_measured_in_every_unit(clock, config: AppConfig):
    c = _detect(clock, config, LEAD_IN(16) + BULL_FVG, opening_range=20.0)[0]
    assert c.type_a.gap_width == 4.0
    assert c.type_a.gap_ticks == 16.0                       # 4.0 / 0.25
    assert c.type_a.normalized_gap == pytest.approx(4.0 / c.atr_at_formation)
    assert c.type_a.gap_pct_candle2_range == pytest.approx(4.0 / 9.25)
    assert c.type_a.gap_pct_opening_range == pytest.approx(0.2)


def test_degenerate_body_void_is_safe(clock, config: AppConfig):
    """Overlapping bodies make body_void <= 0: report 0.0, never divide by it."""
    ohlc = LEAD_IN(16) + [
        (B + 2, B + 5, B + 1, B + 4.5),        # body top B+4.5
        (B + 4.5, B + 13, B + 4.25, B + 12),
        (B + 12, B + 15, B + 9, B + 4.0),      # closes back below candle 1's body top
    ]
    c = _detect(clock, config, ohlc)[0]
    assert c.type_a.body_void <= 0
    assert c.type_a.body_void_degenerate
    assert c.type_a.preservation_ratio == 0.0
    assert not c.type_a.passed
