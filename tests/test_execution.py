from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fvg_backtest.config.schema import BufferConfig, EntryConfig, InversionConfig, ZoneConfig
from fvg_backtest.execution.costs import CostModel
from fvg_backtest.execution.intrabar import IntrabarResolver, SequenceAmbiguity
from fvg_backtest.execution.orders import (
    CancelReason,
    build_inversion_order,
    build_original_order,
    order_age_bucket,
    stop_price_for,
)
from fvg_backtest.fvg.detector import detect_candidates
from fvg_backtest.fvg.zone import ZoneStateMachine
from helpers import bars_from, flat_bars

NY = ZoneInfo("America/New_York")
B = 21000.0
T = datetime(2025, 1, 6, 9, 40, tzinfo=NY)


def _bar(o, h, l, c, minute=0):
    return {
        "open": o, "high": h, "low": l, "close": c,
        "timestamp_ny": T + timedelta(minutes=minute),
    }


# --------------------------------------------------------------------------
# tick rounding & costs
# --------------------------------------------------------------------------


def test_tick_rounding(config):
    nq = CostModel(config.instruments["NQ"])
    assert nq.round_to_tick(21000.13) == 21000.25
    assert nq.round_to_tick(21000.12) == 21000.0
    assert nq.round_up(21000.01) == 21000.25
    assert nq.round_down(21000.24) == 21000.0
    assert nq.tick_value == 5.0


def test_nq_and_mnq_costs_differ(config):
    nq = CostModel(config.instruments["NQ"])
    mnq = CostModel(config.instruments["MNQ"])
    assert nq.points_to_dollars(10) == 200.0
    assert mnq.points_to_dollars(10) == 20.0
    assert nq.round_turn_fees != mnq.round_turn_fees
    # the same points move is a much bigger share of fees on the micro
    assert mnq.fees_in_points() > nq.fees_in_points()


def test_slippage_always_adverse(config):
    nq = CostModel(config.instruments["NQ"])
    # long: stop fills lower, target no better than the level
    assert nq.stop_fill(21000.0, "LONG") < 21000.0
    assert nq.target_fill(21000.0, "LONG") <= 21000.0
    # short: stop fills higher
    assert nq.stop_fill(21000.0, "SHORT") > 21000.0
    assert nq.market_exit_fill(21000.0, "SHORT") > 21000.0


def test_gross_and_net_reported_separately(config):
    nq = CostModel(config.instruments["NQ"])
    gross = nq.points_to_dollars(8.0)
    net = nq.net_dollars(8.0)
    assert gross == 160.0
    assert net == pytest.approx(160.0 - nq.round_turn_fees)
    assert net < gross


# --------------------------------------------------------------------------
# conservative intrabar sequencing
# --------------------------------------------------------------------------


CONS = IntrabarResolver("ONE_MINUTE_CONSERVATIVE")


def test_entry_then_stop_in_the_same_candle():
    ev = CONS.resolve(
        _bar(B + 12, B + 12, B - 2, B),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=False,
    )
    assert ev.kinds == ["ENTRY", "STOP"]
    assert ev.ambiguity == SequenceAmbiguity.ENTRY_STOP_TARGET_SAME_BAR or ev.ambiguous
    # the assumed path runs straight to the stop: no favourable credit
    assert ev.excursion_high == B + 9
    assert ev.excursion_low == B + 1


def test_stop_before_target_when_both_hit_after_entry():
    ev = CONS.resolve(
        _bar(B + 10, B + 20, B - 2, B + 15),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=True,
    )
    assert ev.kinds == ["STOP"]
    assert ev.ambiguity == SequenceAmbiguity.STOP_AND_TARGET_SAME_BAR
    assert ev.excursion_high == B + 15  # anchored at the close, never the target


def test_target_only_fills_normally():
    ev = CONS.resolve(
        _bar(B + 10, B + 20, B + 8, B + 19),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=True,
    )
    assert ev.kinds == ["TARGET"]
    assert not ev.ambiguous
    assert ev.excursion_high == B + 18


def test_entry_and_target_same_bar_is_flagged():
    ev = CONS.resolve(
        _bar(B + 10, B + 20, B + 8, B + 19),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=False,
    )
    assert ev.kinds == ["ENTRY", "TARGET"]
    assert ev.ambiguity == SequenceAmbiguity.ENTRY_AND_TARGET_SAME_BAR


def test_entry_bar_only_credits_provable_excursion():
    # a long fills on the way down: the bar's low is certainly post-entry,
    # its high may have happened before the fill
    ev = CONS.resolve(
        _bar(B + 12, B + 14, B + 7, B + 10),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 30,
        position_open=False,
    )
    assert ev.kinds == ["ENTRY"]
    assert ev.excursion_low == B + 7
    assert ev.excursion_high == B + 10       # the close, not the B+14 high


def test_short_side_mirrors():
    ev = CONS.resolve(
        _bar(B - 12, B - 7, B - 14, B - 10),
        direction="SHORT", entry=B - 9, stop=B - 1, target=B - 30,
        position_open=False,
    )
    assert ev.kinds == ["ENTRY"]
    assert ev.excursion_high == B - 7
    assert ev.excursion_low == B - 10

    ev = CONS.resolve(
        _bar(B - 10, B + 2, B - 20, B - 15),
        direction="SHORT", entry=B - 9, stop=B - 1, target=B - 18,
        position_open=True,
    )
    assert ev.kinds == ["STOP"]              # stop first, adverse assumption


def test_no_events_when_flat_and_untouched():
    ev = CONS.resolve(
        _bar(B + 12, B + 13, B + 11, B + 12),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=False,
    )
    assert ev.sequence == []
    assert ev.excursion_low is None


# --------------------------------------------------------------------------
# one-second / tick sequencing
# --------------------------------------------------------------------------


def _seconds(prices, minute=0):
    base = T + timedelta(minutes=minute)
    return [
        {
            "timestamp_ny": base + timedelta(seconds=i),
            "open": p, "high": p, "low": p, "close": p,
        }
        for i, p in enumerate(prices)
    ]


def test_one_second_resolves_target_first_when_it_really_came_first():
    resolver = IntrabarResolver("ONE_SECOND_INTRABAR")
    # price dips to the entry, runs to the target, only then to the stop
    path = [B + 12, B + 9, B + 12, B + 18, B + 10, B + 1]
    ev = resolver.resolve(
        _bar(B + 12, B + 18, B + 1, B + 1),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=False, finer=_seconds(path),
    )
    assert ev.kinds == ["ENTRY", "TARGET"]     # conservative mode would say STOP
    assert not ev.ambiguous
    assert ev.resolution == "ONE_SECOND_INTRABAR"


def test_one_second_confirms_stop_first_when_that_is_the_truth():
    resolver = IntrabarResolver("ONE_SECOND_INTRABAR")
    path = [B + 12, B + 9, B + 5, B + 1, B + 18]
    ev = resolver.resolve(
        _bar(B + 12, B + 18, B + 1, B + 18),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=False, finer=_seconds(path),
    )
    assert ev.kinds == ["ENTRY", "STOP"]
    assert not ev.ambiguous


def test_ambiguity_inside_a_single_second_falls_back_to_conservative():
    resolver = IntrabarResolver("ONE_SECOND_INTRABAR")
    rows = [
        {"timestamp_ny": T, "open": B + 12, "high": B + 12, "low": B + 12, "close": B + 12},
        # one second spanning both levels
        {"timestamp_ny": T + timedelta(seconds=1), "open": B + 9, "high": B + 18,
         "low": B + 1, "close": B + 9},
    ]
    ev = resolver.resolve(
        _bar(B + 12, B + 18, B + 1, B + 9),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=True, finer=rows,
    )
    assert ev.kinds == ["STOP"]
    assert ev.ambiguity == SequenceAmbiguity.STOP_AND_TARGET_SAME_BAR


def test_tick_rows_are_accepted():
    resolver = IntrabarResolver("TICK_INTRABAR")
    ticks = [
        {"timestamp_ny": T + timedelta(seconds=i), "price": p}
        for i, p in enumerate([B + 12, B + 9, B + 14, B + 18])
    ]
    ev = resolver.resolve(
        _bar(B + 12, B + 18, B + 9, B + 18),
        direction="LONG", entry=B + 9, stop=B + 1, target=B + 18,
        position_open=False, finer=ticks,
    )
    assert ev.kinds == ["ENTRY", "TARGET"]


# --------------------------------------------------------------------------
# orders
# --------------------------------------------------------------------------


def _zone(clock, config, session):
    ohlc = flat_bars(16, price=B + 2.5, rng=1.75) + [
        (B + 2, B + 5, B + 1, B + 4),
        (B + 4, B + 13, B + 3.75, B + 12),
        (B + 10.5, B + 15, B + 9, B + 11.5),
    ]
    bars = bars_from(clock, session, "09:15", ohlc)
    cands = detect_candidates(
        bars, config, session,
        cash_open=clock.cash_open_dt(session),
        search_start=clock.fvg_search_start_dt(session),
        search_end=clock.fvg_search_end_dt(session),
    )
    return ZoneStateMachine(fvg=cands[0], config=ZoneConfig(), tick_size=0.25)


def test_original_bullish_order_levels(clock, config, jan6):
    zone = _zone(clock, config, jan6)
    order = build_original_order(
        zone, entry_config=EntryConfig(), target=B + 18,
        created_at=T, activated_at=T + timedelta(minutes=1),
    )
    assert order.direction == "LONG"
    assert order.entry == B + 9        # proximal edge = fvg_high
    assert order.stop == B + 1         # candle 1 low
    assert order.risk_points == 8.0
    assert order.risk_ticks(0.25) == 32.0
    assert order.target_r() == pytest.approx(9.0 / 8.0)
    # candle 3 cannot fill the order it created
    assert order.activated_at > order.created_at


def test_entry_models(clock, config, jan6):
    zone = _zone(clock, config, jan6)
    for model, expected in (
        ("PROXIMAL_EDGE", B + 9), ("MIDPOINT", B + 7), ("DISTAL_EDGE", B + 5)
    ):
        order = build_original_order(
            zone, entry_config=EntryConfig(model=model), target=B + 18,
            created_at=T, activated_at=T + timedelta(minutes=1),
        )
        assert order.entry == expected


def test_inversion_entry_uses_the_opposite_edge(clock, config, jan6):
    zone = _zone(clock, config, jan6)
    zone.update(_bar(B + 8, B + 9, B + 3, B + 4))       # close below -> inversion
    assert zone.state.trade_direction == "SHORT"
    order = build_inversion_order(
        zone, config=InversionConfig(), target=B - 5,
        created_at=T, activated_at=T + timedelta(minutes=1),
        tick=0.25, atr=4.0, inversion_bar=_bar(B + 8, B + 9, B + 3, B + 4),
    )
    assert order.direction == "SHORT"
    assert order.entry == B + 5                          # the far edge, approached from below
    assert order.stop == B + 10                          # opposite edge + 4 ticks
    assert order.kind == "INVERSION"


@pytest.mark.parametrize(
    "model,expected",
    [
        ("OPPOSITE_FVG_EDGE_PLUS_BUFFER", B + 10),      # fvg_high + 1.0
        ("INVERSION_CANDLE_EXTREME_PLUS_BUFFER", B + 10),  # inversion bar high + 1.0
        ("ORIGINAL_CANDLE_1_EXTREME", B + 5),           # candle 1 high
        ("MOST_RECENT_SWING_PLUS_BUFFER", B + 13),      # supplied swing + 1.0
    ],
)
def test_every_inversion_stop_model(clock, config, jan6, model, expected):
    zone = _zone(clock, config, jan6)
    inv_bar = _bar(B + 8, B + 9, B + 3, B + 4)
    zone.update(inv_bar)
    stop = stop_price_for(
        zone, model=model, direction="SHORT", buffer=BufferConfig(unit="ticks", ticks=4),
        tick=0.25, atr=4.0, inversion_bar=inv_bar, recent_swing=B + 12,
    )
    assert stop == expected


def test_stop_buffer_units(clock, config, jan6):
    zone = _zone(clock, config, jan6)
    zone.update(_bar(B + 8, B + 9, B + 3, B + 4))
    common = dict(
        zone=zone, model="OPPOSITE_FVG_EDGE_PLUS_BUFFER", direction="SHORT",
        tick=0.25, atr=4.0,
    )
    assert stop_price_for(**common, buffer=BufferConfig(unit="ticks", ticks=8)) == B + 11
    assert stop_price_for(**common, buffer=BufferConfig(unit="points", points=3)) == B + 12
    assert stop_price_for(**common, buffer=BufferConfig(unit="atr", atr=0.5)) == B + 11


def test_unusable_stop_produces_no_order(clock, config, jan6):
    zone = _zone(clock, config, jan6)
    zone.update(_bar(B + 8, B + 9, B + 3, B + 4))
    cfg = InversionConfig(stop_model="MOST_RECENT_SWING_PLUS_BUFFER")
    # a "recent swing" below the short entry would put the stop on the wrong side
    order = build_inversion_order(
        zone, config=cfg, target=B - 5, created_at=T,
        activated_at=T + timedelta(minutes=1), tick=0.25, atr=4.0, recent_swing=B,
    )
    assert order is None


def test_order_age_buckets():
    assert order_age_bucket(0) == "0-1 minute"
    assert order_age_bucket(1) == "0-1 minute"
    assert order_age_bucket(3) == "2-3 minutes"
    assert order_age_bucket(5) == "4-5 minutes"
    assert order_age_bucket(9) == "6-10 minutes"
    assert order_age_bucket(12) == "11-15 minutes"
    assert order_age_bucket(25) == "16-30 minutes"
    assert order_age_bucket(120) == "31+ minutes"
    assert order_age_bucket(None) is None


def test_order_cancel_records_reason(clock, config, jan6):
    zone = _zone(clock, config, jan6)
    order = build_original_order(
        zone, entry_config=EntryConfig(), target=B + 18,
        created_at=T, activated_at=T + timedelta(minutes=1),
    )
    order.cancel(T + timedelta(minutes=10), CancelReason.TARGET_SWEPT)
    row = order.to_dict(0.25)
    assert row["order_state"] == "CANCELLED"
    assert row["cancel_reason"] == "TARGET_SWEPT"
    assert row["filled_at"] is None
