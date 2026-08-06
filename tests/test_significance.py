from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fvg_backtest.config.schema import TypeAConfig, TypeBConfig
from fvg_backtest.fvg.significance import evaluate_type_a, evaluate_type_b

NY = ZoneInfo("America/New_York")
B = 21000.0
C1_TIME = datetime(2025, 1, 6, 9, 31, tzinfo=NY)
CASH_OPEN = datetime(2025, 1, 6, 9, 30, tzinfo=NY)


def _bar(minutes_before, o, h, l, c, atr=4.0):
    return {
        "timestamp_ny": C1_TIME - timedelta(minutes=minutes_before),
        "open": o, "high": h, "low": l, "close": c, "atr": atr,
    }


# --------------------------------------------------------------------------
# Type A
# --------------------------------------------------------------------------


def _type_a(gap=4.0, atr=4.0, void=6.5, cfg=None, c2_range=9.25):
    """Bullish helper: body void placed explicitly."""
    return evaluate_type_a(
        direction="BULLISH",
        gap_width=gap,
        atr=atr,
        c1_body_top=B + 4,
        c1_body_bottom=B + 2,
        c3_body_top=B + 15,
        c3_body_bottom=B + 4 + void,
        candle2_range=c2_range,
        tick_size=0.25,
        config=cfg or TypeAConfig(),
    )


def test_type_a_formula():
    r = _type_a(gap=4.0, atr=4.0, void=6.5)
    assert r.normalized_gap == pytest.approx(1.0)          # 4.0 / 4.0
    assert r.body_void == pytest.approx(6.5)
    assert r.preservation_ratio == pytest.approx(4.0 / 6.5)
    assert r.passed


def test_type_a_fails_on_small_gap():
    r = _type_a(gap=0.3, atr=10.0, void=0.5)               # 0.03 ATR
    assert r.normalized_gap == pytest.approx(0.03)
    assert not r.passed


def test_type_a_fails_when_wicks_nearly_closed_the_gap():
    # a wide body void relative to the gap means the wicks ate most of it
    r = _type_a(gap=4.0, atr=4.0, void=20.0)
    assert r.preservation_ratio == pytest.approx(0.2)
    assert not r.passed


def test_type_a_thresholds_are_configurable():
    strict = TypeAConfig(minimum_gap_atr=1.5, minimum_preservation_ratio=0.5)
    assert not _type_a(gap=4.0, atr=4.0, void=6.5, cfg=strict).passed
    loose = TypeAConfig(minimum_gap_atr=0.01, minimum_preservation_ratio=0.05)
    assert _type_a(gap=4.0, atr=4.0, void=20.0, cfg=loose).passed


def test_type_a_zero_atr_and_negative_void_are_safe():
    r = _type_a(gap=4.0, atr=0.0, void=6.5)
    assert r.normalized_gap == 0.0 and not r.passed
    r = _type_a(gap=4.0, atr=4.0, void=-2.0)
    assert r.body_void_degenerate and r.preservation_ratio == 0.0


def test_type_a_bearish_body_void_direction():
    r = evaluate_type_a(
        direction="BEARISH",
        gap_width=4.0, atr=4.0,
        c1_body_top=B - 2, c1_body_bottom=B - 4,
        c3_body_top=B - 10.5, c3_body_bottom=B - 15,
        candle2_range=9.25, tick_size=0.25, config=TypeAConfig(),
    )
    assert r.body_void == pytest.approx(6.5)               # c1_bottom - c3_top
    assert r.preservation_ratio == pytest.approx(4.0 / 6.5)


def test_type_a_records_all_width_units():
    r = _type_a(gap=4.0, atr=4.0, void=6.5, c2_range=8.0)
    assert r.gap_ticks == 16.0
    assert r.gap_pct_candle2_range == pytest.approx(0.5)
    r2 = evaluate_type_a(
        direction="BULLISH", gap_width=4.0, atr=4.0,
        c1_body_top=B + 4, c1_body_bottom=B + 2,
        c3_body_top=B + 15, c3_body_bottom=B + 10.5,
        candle2_range=9.25, tick_size=0.25, config=TypeAConfig(),
        opening_range=16.0,
    )
    assert r2.gap_pct_opening_range == pytest.approx(0.25)


# --------------------------------------------------------------------------
# Type B
# --------------------------------------------------------------------------

ZONE = dict(fvg_low=B + 5, fvg_high=B + 9, gap_width=4.0)


def _type_b(bars, cfg=None):
    return evaluate_type_b(
        prior_bars=bars,
        **ZONE,
        candle1_time=C1_TIME,
        cash_open=CASH_OPEN,
        tick_size=0.25,
        config=cfg or TypeBConfig(),
    )


def test_type_b_qualifying_wick():
    # upper wick B+3.25 -> B+7: length 3.75, range 4.25, overlap 2.0
    r = _type_b([_bar(6, B + 3, B + 7, B + 2.75, B + 3.25)])
    assert r.passed
    w = r.closest
    assert w.side == "UPPER"
    assert w.wick_points == pytest.approx(3.75)
    assert w.wick_ticks == pytest.approx(15.0)
    assert w.wick_share == pytest.approx(3.75 / 4.25)
    assert w.wick_atr_ratio == pytest.approx(3.75 / 4.0)
    assert w.overlap_points == pytest.approx(2.0)
    assert w.overlap_ticks == pytest.approx(8.0)
    assert w.fvg_overlap_ratio == pytest.approx(0.5)
    assert w.minutes_before_candle1 == 6
    assert w.candle_direction == "BULLISH"


def test_type_b_wick_must_overlap_the_zone():
    # a long wick that stops short of the zone does not qualify
    r = _type_b([_bar(3, B, B + 4.5, B - 0.25, B + 0.25)])
    assert not r.passed
    assert r.wicks[0].overlap_points == 0.0
    assert r.wicks[0].fvg_overlap_ratio == 0.0


def test_type_b_wick_must_be_long_enough_absolutely_and_relatively():
    # overlaps the zone but is a small share of a huge candle
    r = _type_b([_bar(2, B - 10, B + 6, B - 11, B + 5.5)])
    w = next(x for x in r.wicks if x.side == "UPPER")
    assert w.overlap_points > 0
    assert w.wick_share < 0.40
    assert not r.passed

    # long share but tiny in ATR terms
    r = _type_b([_bar(2, B + 5.9, B + 6.0, B + 5.85, B + 5.9)], TypeBConfig(minimum_wick_atr=0.5))
    assert not r.passed


def test_type_b_lower_wick_of_bearish_candle():
    r = _type_b([_bar(4, B + 12, B + 12.5, B + 7, B + 11)])
    w = r.closest
    assert w.side == "LOWER"
    assert w.candle_direction == "BEARISH"
    assert w.overlap_points == pytest.approx(2.0)   # B+7 .. B+9
    assert r.passed


def test_type_b_reports_closest_and_largest_separately():
    bars = [
        _bar(12, B + 3, B + 8.5, B + 2.75, B + 3.25),   # older, larger wick
        _bar(4, B + 4, B + 6.5, B + 3.9, B + 4.2),      # newer, smaller wick
    ]
    r = _type_b(bars)
    assert r.passed
    assert r.qualifying_count == 2
    assert r.closest.minutes_before_candle1 == 4
    assert r.largest.wick_points == pytest.approx(5.25)
    assert r.largest.minutes_before_candle1 == 12


def test_type_b_tracks_wicks_before_and_after_the_cash_open():
    before = _bar(6, B + 3, B + 7, B + 2.75, B + 3.25)   # 9:25 -> premarket
    after = _bar(1, B + 3, B + 7, B + 2.75, B + 3.25)    # 9:30 -> cash
    r = _type_b([before, after])
    assert r.any_qualifying_before_open
    assert r.any_qualifying_after_open
    assert [w.after_cash_open for w in r.wicks if w.qualifies] == [False, True]


def test_type_b_no_prior_bars():
    r = _type_b([])
    assert not r.passed and r.examined_count == 0 and r.closest is None


def test_type_b_thresholds_are_configurable():
    bar = [_bar(6, B + 3, B + 7, B + 2.75, B + 3.25)]
    assert _type_b(bar).passed
    assert not _type_b(bar, TypeBConfig(minimum_fvg_overlap_ratio=0.9)).passed
    assert not _type_b(bar, TypeBConfig(minimum_wick_share=0.95)).passed
    assert not _type_b(bar, TypeBConfig(minimum_wick_atr=1.5)).passed
