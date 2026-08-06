from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fvg_backtest.config.schema import EqualLevelsConfig, LiquidityConfig, TargetConfig
from fvg_backtest.liquidity import PivotSide, PivotStatus, PivotTracker
from fvg_backtest.liquidity.targets import build_clusters, select_target

NY = ZoneInfo("America/New_York")
B = 21000.0
T0 = datetime(2025, 1, 6, 9, 0, tzinfo=NY)


def _bars(highs_lows, start=T0, segment="CASH"):
    return [
        {
            "timestamp_ny": start + timedelta(minutes=i),
            "high": h, "low": l, "open": (h + l) / 2, "close": (h + l) / 2,
            "session_segment": segment,
        }
        for i, (h, l) in enumerate(highs_lows)
    ]


def _feed(tracker, bars):
    confirmed = []
    for b in bars:
        confirmed.append(tracker.push(b))
    return confirmed


# --------------------------------------------------------------------------
# pivot confirmation
# --------------------------------------------------------------------------


def test_1x1_swing_high_confirms_only_after_the_next_bar(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    bars = _bars([(B + 1, B - 1), (B + 5, B + 1), (B + 2, B), (B + 1, B - 1)])
    per_bar = _feed(tr, bars)
    # bar index 1 is the pivot; nothing is confirmed until bar index 2 closes
    assert per_bar[0] == [] and per_bar[1] == []
    highs = [p for p in per_bar[2] if p.side == PivotSide.HIGH]
    assert len(highs) == 1
    p = highs[0]
    assert p.price == B + 5
    assert p.bar_index == 1
    assert p.timestamp == T0 + timedelta(minutes=1)
    assert p.confirmed_at == T0 + timedelta(minutes=2)   # no lookahead


def test_1x1_swing_low(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    per_bar = _feed(tr, _bars([(B + 1, B - 1), (B, B - 5), (B + 1, B - 2)]))
    lows = [p for p in per_bar[2] if p.side == PivotSide.LOW]
    assert len(lows) == 1 and lows[0].price == B - 5


def test_strict_left_nonstrict_right_shoulder(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    # equal high on the right still confirms; equal on the left does not
    per_bar = _feed(tr, _bars([(B + 1, B), (B + 5, B), (B + 5, B), (B + 1, B)]))
    highs = [p for group in per_bar for p in group if p.side == PivotSide.HIGH]
    assert [p.bar_index for p in highs] == [1]


def test_2x2_and_3x3_pivots(clock):
    # peak at index 3, monotonically falling lows so only the high pivots
    pattern = _bars([
        (B + 1, B - 1), (B + 2, B - 2), (B + 3, B - 3), (B + 9, B - 4),
        (B + 3, B - 5), (B + 2, B - 6), (B + 1, B - 7), (B + 1, B - 8),
    ])
    tr2 = PivotTracker(LiquidityConfig(pivot_strength=2))
    per_bar = _feed(tr2, pattern)
    highs = [p for g in per_bar for p in g if p.side == PivotSide.HIGH]
    assert len(highs) == 1
    assert highs[0].bar_index == 3
    assert highs[0].strength == 2
    assert highs[0].confirmed_at == T0 + timedelta(minutes=5)   # i + 2

    tr3 = PivotTracker(LiquidityConfig(pivot_strength=3))
    per_bar = _feed(tr3, pattern)
    highs = [p for g in per_bar for p in g if p.side == PivotSide.HIGH]
    assert len(highs) == 1
    assert highs[0].bar_index == 3
    assert highs[0].confirmed_at == T0 + timedelta(minutes=6)   # i + 3


def test_no_lookahead_pivot_never_confirmed_early(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=2))
    bars = _bars([(B + 1, B), (B + 2, B), (B + 9, B), (B + 3, B)])
    per_bar = _feed(tr, bars)
    # the 4th bar (i+1) is not enough for a 2x2 pivot
    assert all(len(g) == 0 for g in per_bar)


# --------------------------------------------------------------------------
# sweeps
# --------------------------------------------------------------------------


def test_swing_high_becomes_swept(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([(B + 1, B), (B + 5, B), (B + 2, B), (B + 6, B)]))
    p = tr.pivots[0]
    assert p.status == PivotStatus.SWEPT
    assert p.is_swept and not p.eligible
    assert p.swept_at == T0 + timedelta(minutes=3)


def test_exact_touch_is_not_a_sweep_by_default(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([(B + 1, B), (B + 5, B), (B + 2, B), (B + 5, B)]))
    p = tr.pivots[0]
    assert p.status == PivotStatus.TOUCHED_ONCE
    assert p.exact_touch_count == 1
    assert p.eligible                       # touched-but-not-swept stays eligible

    tr2 = PivotTracker(LiquidityConfig(pivot_strength=1, count_exact_touch_as_sweep=True))
    _feed(tr2, _bars([(B + 1, B), (B + 5, B), (B + 2, B), (B + 5, B)]))
    assert tr2.pivots[0].status == PivotStatus.SWEPT


def test_sweep_tolerance_in_ticks(clock):
    cfg = LiquidityConfig(pivot_strength=1, sweep_tolerance_ticks=4)  # 1.00 point
    tr = PivotTracker(cfg)
    _feed(tr, _bars([(B + 1, B), (B + 5, B), (B + 2, B), (B + 5.75, B)]))
    assert tr.pivots[0].status == PivotStatus.TOUCHED_ONCE   # inside tolerance
    _feed(tr, _bars([(B + 6.5, B)], start=T0 + timedelta(minutes=4)))
    assert tr.pivots[0].status == PivotStatus.SWEPT


def test_multiple_touches_tracked(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([(B + 1, B), (B + 5, B), (B + 2, B), (B + 5, B), (B + 5, B)]))
    p = tr.pivots[0]
    assert p.touch_count == 2
    assert p.status == PivotStatus.TOUCHED_MULTIPLE_TIMES


# --------------------------------------------------------------------------
# target selection
# --------------------------------------------------------------------------


def _select(tr, direction, entry, now, **kw):
    return select_target(
        tr,
        direction=direction,
        entry=entry,
        now=now,
        liquidity_config=kw.pop("liq", LiquidityConfig()),
        target_config=kw.pop("tgt", TargetConfig()),
        equal_levels=kw.pop("eq", EqualLevelsConfig()),
        tick=0.25,
        atr=kw.pop("atr", 4.0),
    )


def test_nearest_untaken_high_is_selected_not_the_newest(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([
        (B + 1, B), (B + 40, B), (B + 2, B),       # oldest pivot   B+40
        (B + 3, B), (B + 20, B), (B + 4, B),       # middle pivot   B+20
        (B + 1, B), (B + 5, B),  (B + 2, B),       # newest pivot   B+5
    ]))
    now = T0 + timedelta(minutes=20)
    sel = _select(tr, "LONG", B + 9, now)
    assert sel.found
    # the newest swing (B+5) sits below the entry, so the nearest *reachable*
    # level wins — selection is by distance, never recency
    assert sel.price == B + 20
    assert sel.label == "SWING_HIGH"
    assert len(sel.candidates) == 3


def test_swept_targets_are_excluded(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([
        (B + 1, B), (B + 15, B), (B + 2, B),
        (B + 16, B),                              # sweeps B+15
        (B + 3, B), (B + 30, B), (B + 4, B),
    ]))
    now = T0 + timedelta(minutes=20)
    sel = _select(tr, "LONG", B + 9, now)
    assert sel.price == B + 30                     # B+15 was swept


def test_target_must_be_beyond_the_entry(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([(B + 1, B), (B + 8, B), (B + 2, B), (B + 20, B - 1), (B + 3, B)]))
    now = T0 + timedelta(minutes=20)
    sel = _select(tr, "LONG", B + 9, now)
    assert sel.price == B + 20                     # B+8 sits below the entry


def test_no_target_is_reported_not_invented(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([(B + 1, B), (B + 5, B), (B + 2, B)]))
    now = T0 + timedelta(minutes=20)
    sel = _select(tr, "LONG", B + 9, now)
    assert not sel.found
    assert sel.price is None
    assert sel.label == "NO_TARGET"
    row = sel.to_dict(now=now, entry=B + 9, tick=0.25, atr=4.0, risk=8.0)
    assert row["target_found"] is False and row["target_price"] is None


def test_target_age_window_is_enforced(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([(B + 1, B), (B + 15, B), (B + 2, B)]))
    # 2 minutes after the pivot: too fresh (min age 5)
    early = _select(tr, "LONG", B + 9, T0 + timedelta(minutes=3))
    assert not early.found
    # 10 minutes later: eligible
    ok = _select(tr, "LONG", B + 9, T0 + timedelta(minutes=11))
    assert ok.found and ok.price == B + 15
    # 90 minutes later: outside the 60-minute lookback
    stale = _select(tr, "LONG", B + 9, T0 + timedelta(minutes=91))
    assert not stale.found


def test_bearish_target_is_nearest_low_below_entry(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([
        (B, B - 1), (B, B - 30), (B, B - 2),
        (B, B - 3), (B, B - 15), (B, B - 4),
    ]))
    now = T0 + timedelta(minutes=20)
    sel = _select(tr, "SHORT", B - 9, now)
    assert sel.found and sel.price == B - 15
    assert sel.label == "SWING_LOW"


def test_target_metrics_row(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([(B + 1, B), (B + 17, B), (B + 2, B)], segment="PREMARKET"))
    now = T0 + timedelta(minutes=11)
    sel = _select(tr, "LONG", B + 9, now)
    row = sel.to_dict(now=now, entry=B + 9, tick=0.25, atr=4.0, risk=8.0)
    assert row["target_price"] == B + 17
    assert row["target_distance_points"] == pytest.approx(8.0)
    assert row["target_distance_ticks"] == pytest.approx(32.0)
    assert row["target_distance_atr"] == pytest.approx(2.0)
    assert row["target_distance_r"] == pytest.approx(1.0)
    assert row["target_age_minutes"] == 10
    assert row["target_pivot_strength"] == 1
    assert row["target_session_segment"] == "PREMARKET"


def test_intervening_liquidity_counted(clock):
    """A swing low sitting between the entry and the chosen high target."""
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([
        (B + 31, B + 25), (B + 45, B + 26), (B + 32, B + 27),  # high pivot B+45
        (B + 29, B + 20), (B + 28, B + 21),                    # low pivot  B+20
        (B + 30, B + 22), (B + 27, B + 23), (B + 26, B + 24),  # high pivot B+30
    ]))
    now = T0 + timedelta(minutes=20)
    prices = sorted(p.price for p in tr.pivots)
    assert prices == [B + 20, B + 30, B + 45]

    sel = _select(tr, "LONG", B + 9, now)
    assert sel.price == B + 30                    # nearest reachable high
    assert sel.intervening_levels == 1            # the B+20 swing low is in the way

    higher = _select(tr, "LONG", B + 31, now)
    assert higher.price == B + 45
    assert higher.intervening_levels == 0         # clear path above B+31


# --------------------------------------------------------------------------
# equal highs / lows
# --------------------------------------------------------------------------


def test_equal_highs_cluster_within_tolerance(clock):
    # a 2-tick sweep tolerance lets a 1-tick-higher retest form equal highs
    # instead of sweeping the first one
    liq = LiquidityConfig(pivot_strength=1, sweep_tolerance_ticks=2)
    tr = PivotTracker(liq)
    _feed(tr, _bars([
        (B + 1, B), (B + 25, B), (B + 2, B),         # far pivot   B+25
        (B + 3, B), (B + 15, B), (B + 4, B),         # pivot       B+15
        (B + 5, B), (B + 15.25, B), (B + 6, B),      # 1 tick up -> equal highs
    ]))
    now = T0 + timedelta(minutes=20)
    highs = [p for p in tr.pivots if p.side == PivotSide.HIGH]
    assert all(p.eligible for p in highs)            # nothing was swept

    clusters = build_clusters(highs, EqualLevelsConfig(tolerance_ticks=2), 0.25, 4.0)
    assert sorted(c.size for c in clusters) == [1, 2]
    pair = next(c for c in clusters if c.size == 2)
    assert pair.spread == pytest.approx(0.25)
    assert pair.price == B + 15.25                   # reference = the extreme
    assert pair.retest_count >= 1

    sel = _select(tr, "LONG", B + 9, now, liq=liq, eq=EqualLevelsConfig(tolerance_ticks=2))
    assert sel.price == B + 15
    assert sel.cluster is not None and sel.cluster.size == 2
    row = sel.to_dict(now=now, entry=B + 9, tick=0.25, atr=4.0, risk=8.0)
    assert row["target_is_cluster"] is True
    assert row["target_cluster_size"] == 2
    assert row["target_cluster_spread"] == pytest.approx(0.25)
    assert row["target_cluster_oldest_age"] == 16
    assert row["target_cluster_newest_age"] == 13


def test_atr_tolerance_mode(clock):
    tr = PivotTracker(LiquidityConfig(pivot_strength=1))
    _feed(tr, _bars([
        (B + 1, B), (B + 15, B), (B + 2, B),
        (B + 3, B), (B + 15.5, B), (B + 4, B),
    ]))
    highs = [p for p in tr.pivots if p.side == PivotSide.HIGH]
    tight = build_clusters(highs, EqualLevelsConfig(tolerance_mode="atr", tolerance_atr=0.02), 0.25, 4.0)
    assert sorted(c.size for c in tight) == [1, 1]     # 0.08 points tolerance
    loose = build_clusters(highs, EqualLevelsConfig(tolerance_mode="atr", tolerance_atr=0.05), 0.25, 20.0)
    assert sorted(c.size for c in loose) == [2]        # 1.0 point tolerance
