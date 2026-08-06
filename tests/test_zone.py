from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fvg_backtest.config.schema import ZoneConfig
from fvg_backtest.fvg import ZoneState, ZoneStateMachine
from fvg_backtest.fvg.detector import detect_candidates
from helpers import bars_from, flat_bars

NY = ZoneInfo("America/New_York")
SESSION = date(2025, 1, 6)
B = 21000.0

LEAD_IN = lambda n: flat_bars(n, price=B + 2.5, rng=1.75)
BULL_FVG = [
    (B + 2, B + 5, B + 1, B + 4),
    (B + 4, B + 13, B + 3.75, B + 12),
    (B + 10.5, B + 15, B + 9, B + 11.5),
]
BEAR_FVG = [
    (B - 2, B - 1, B - 5, B - 4),
    (B - 4, B - 3.75, B - 13, B - 12),
    (B - 10.5, B - 9, B - 15, B - 11.5),
]


def _zone(clock, config, fvg_bars=BULL_FVG, lead=None):
    ohlc = (lead if lead is not None else LEAD_IN(16)) + fvg_bars
    bars = bars_from(clock, SESSION, "09:15", ohlc)
    cands = detect_candidates(
        bars, config, SESSION,
        cash_open=clock.cash_open_dt(SESSION),
        search_start=clock.fvg_search_start_dt(SESSION),
        search_end=clock.fvg_search_end_dt(SESSION),
    )
    return ZoneStateMachine(fvg=cands[0], config=ZoneConfig(), tick_size=0.25)


def _bar(minute, o, h, l, c):
    t = datetime(2025, 1, 6, 9, 34, tzinfo=NY) + timedelta(minutes=minute)
    return {"timestamp_ny": t, "open": o, "high": h, "low": l, "close": c}


def test_initial_state(clock, config):
    z = _zone(clock, config)
    assert z.state == ZoneState.ORIGINAL_BULLISH
    assert z.state.is_bullish
    assert z.state.trade_direction == "LONG"
    assert (z.low, z.high, z.midpoint) == (B + 5, B + 9, B + 7)
    assert z.proximal_edge() == B + 9
    assert z.distal_edge() == B + 5

    zb = _zone(clock, config, BEAR_FVG, lead=flat_bars(16, price=B - 2.5, rng=1.75))
    assert zb.state == ZoneState.ORIGINAL_BEARISH
    assert zb.state.trade_direction == "SHORT"
    assert zb.proximal_edge() == B - 9


def test_mitigation_does_not_invalidate(clock, config):
    z = _zone(clock, config)
    z.update(_bar(0, B + 10, B + 10.5, B + 6.5, B + 10))     # into the zone
    assert z.state == ZoneState.ORIGINAL_BULLISH             # still active
    assert z.touch_count == 1
    assert z.first_touch_time is not None
    assert z.max_penetration == pytest.approx(2.5)           # B+9 -> B+6.5
    assert z.max_penetration_ratio == pytest.approx(0.625)
    assert z.midpoint_touches == 1
    assert z.closes_inside == 0

    z.update(_bar(1, B + 10, B + 11, B + 8, B + 8.5))        # closes inside
    assert z.closes_inside == 1
    assert z.touch_count == 2
    assert z.minutes_inside == 2
    assert z.state == ZoneState.ORIGINAL_BULLISH


def test_deep_penetration_and_distal_touch_tracked(clock, config):
    z = _zone(clock, config)
    z.update(_bar(0, B + 10, B + 10, B + 5, B + 9.5))        # to the distal edge
    assert z.distal_touches == 1
    assert z.max_penetration == pytest.approx(4.0)
    assert z.max_penetration_ratio == pytest.approx(1.0)
    assert z.state == ZoneState.ORIGINAL_BULLISH


def test_wick_completely_through_zone_does_not_invert(clock, config):
    z = _zone(clock, config)
    # low B+3 is below the zone, high B+11 above it, but the close is back inside
    z.update(_bar(0, B + 10, B + 11, B + 3, B + 7))
    assert z.wicks_through == 1
    assert z.state == ZoneState.ORIGINAL_BULLISH             # NOT inverted
    assert z.inversion_count == 0
    assert any(e.kind == "WICK_THROUGH_ZONE" for e in z.events)


def test_close_below_zone_inverts_bullish_to_bearish(clock, config):
    z = _zone(clock, config)
    z.update(_bar(0, B + 10, B + 10.5, B + 6.5, B + 7))      # mitigation only
    assert z.state == ZoneState.ORIGINAL_BULLISH
    events = z.update(_bar(1, B + 7, B + 7.5, B + 3, B + 4)) # closes below B+5
    assert z.state == ZoneState.BEARISH_INVERSION
    assert z.state.trade_direction == "SHORT"
    assert z.inversion_count == 1
    inv = next(e for e in events if e.kind == "INVERSION")
    assert inv.detail["from_state"] == "ORIGINAL_BULLISH"
    assert inv.detail["close_distance_beyond_zone"] == pytest.approx(1.0)
    assert inv.detail["close_distance_beyond_zone_ticks"] == pytest.approx(4.0)
    assert inv.detail["mitigations_before"] == 2
    assert inv.detail["minutes_since_formation"] == 2        # 9:33 -> 9:35
    # after inverting, the entry edge flips to the other side of the zone
    assert z.proximal_edge() == B + 5


def test_close_above_zone_inverts_bearish_to_bullish(clock, config):
    z = _zone(clock, config, BEAR_FVG, lead=flat_bars(16, price=B - 2.5, rng=1.75))
    z.update(_bar(0, B - 10, B - 6.5, B - 10.5, B - 7))
    assert z.state == ZoneState.ORIGINAL_BEARISH
    z.update(_bar(1, B - 7, B - 3, B - 7.5, B - 4))          # closes above B-5
    assert z.state == ZoneState.BULLISH_INVERSION
    assert z.proximal_edge() == B - 5


def test_close_exactly_on_boundary_does_not_invert_by_default(clock, config):
    z = _zone(clock, config)
    z.update(_bar(0, B + 8, B + 9, B + 4, B + 5))            # closes exactly at fvg_low
    assert z.state == ZoneState.ORIGINAL_BULLISH
    assert z.inversion_count == 0

    z2 = _zone(clock, config)
    z2.config = ZoneConfig(invert_on_touch_close=True)
    z2.update(_bar(0, B + 8, B + 9, B + 4, B + 5))
    assert z2.state == ZoneState.BEARISH_INVERSION


def test_repeated_inversions_are_tracked(clock, config):
    z = _zone(clock, config)
    z.update(_bar(0, B + 8, B + 9, B + 3, B + 4))            # -> BEARISH_INVERSION
    assert z.state == ZoneState.BEARISH_INVERSION
    z.update(_bar(1, B + 4, B + 10, B + 4, B + 9.5))         # -> BULLISH_REINVERSION
    assert z.state == ZoneState.BULLISH_REINVERSION
    z.update(_bar(2, B + 9.5, B + 10, B + 4, B + 4.5))       # -> BEARISH_REINVERSION
    assert z.state == ZoneState.BEARISH_REINVERSION
    assert z.inversion_count == 3
    assert z.complete_crossings == 3
    kinds = [e.kind for e in z.events if e.kind in ("INVERSION", "REINVERSION")]
    assert kinds == ["INVERSION", "REINVERSION", "REINVERSION"]
    last = z.events[-1]
    assert last.detail["minutes_since_previous_inversion"] == 1
    assert last.detail["inversion_index"] == 3


def test_midpoint_crossings_counted(clock, config):
    z = _zone(clock, config)
    for i, close in enumerate([B + 8, B + 6, B + 8, B + 6]):
        z.update(_bar(i, close, close + 0.5, close - 0.5, close))
    assert z.midpoint_crossings == 3


def test_summary_contains_every_tracked_field(clock, config):
    z = _zone(clock, config)
    z.update(_bar(0, B + 10, B + 10.5, B + 6.5, B + 7))
    z.update(_bar(1, B + 7, B + 7.5, B + 3, B + 4))
    s = z.summary()
    for key in (
        "zone_state_final", "zone_first_touch_time", "zone_touch_count",
        "zone_max_penetration", "zone_midpoint_touches", "zone_distal_touches",
        "zone_closes_inside", "zone_minutes_inside", "zone_wicks_through",
        "zone_inversion_count", "zone_midpoint_crossings", "zone_complete_crossings",
    ):
        assert key in s
    assert s["zone_state_final"] == "BEARISH_INVERSION"
    assert s["zone_inversion_count"] == 1
