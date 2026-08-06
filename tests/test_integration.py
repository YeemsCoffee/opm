"""End-to-end integration over the deterministic scenario sessions.

Each scenario plants a setup whose correct outcome is known by construction
(see :mod:`fvg_backtest.data.synthetic`), so these tests check the whole
chain: load -> detect -> qualify -> target -> order -> fill -> mitigation ->
inversion -> exit -> metrics -> persisted run directory.
"""

from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from fvg_backtest.config import AppConfig
from fvg_backtest.data.synthetic import SyntheticProvider
from fvg_backtest.pipeline import run_backtest
from fvg_backtest.sessions import SessionClock, TradingCalendar

SESSION = date(2025, 1, 6)
B = 21000.0
# the provider needs the lead-in sessions on the same scenario so the
# warm-up days do not change what the tested session sees
LEAD_IN_DAYS = [date(2025, 1, 2), date(2025, 1, 3), SESSION]


def _run(scenario: str, instrument: str = "NQ", **overrides) -> object:
    cfg = AppConfig(
        instrument=instrument,
        contract_mode="DATED",
        contract=f"{instrument}H25",
        start=SESSION.isoformat(),
        end=SESSION.isoformat(),
    )
    for path, value in overrides.items():
        cfg.set_by_path(path.replace("__", "."), value)
    clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    provider = SyntheticProvider(
        clock, cfg.instruments, schedule={d: scenario for d in LEAD_IN_DAYS}
    )
    return run_backtest(cfg, provider=provider)


# --------------------------------------------------------------------------
# the headline path
# --------------------------------------------------------------------------


def test_bullish_clean_full_lifecycle():
    out = _run("bullish_clean")

    # 1. one significant FVG for the session, and we can see why it qualified
    assert out.setups.height == 1
    setup = out.setups.row(0, named=True)
    assert setup["selected"] is True
    assert setup["direction"] == "BULLISH"
    assert setup["significance_type"] == "A_AND_B"
    assert setup["c1_time"].strftime("%H:%M") == "09:31"
    assert setup["c3_time"].strftime("%H:%M") == "09:33"
    assert setup["fvg_low"] == B + 5 and setup["fvg_high"] == B + 9
    assert setup["gap_width"] == 4.0
    assert setup["type_a_normalized_gap"] >= 0.10
    assert setup["type_a_preservation_ratio"] >= 0.50

    # 2. the qualifying prior wick is identified
    assert setup["type_b_passed"] is True
    assert setup["type_b_closest_wick_timestamp"].strftime("%H:%M") == "09:22"
    assert setup["type_b_closest_wick_side"] == "UPPER"

    # 3. the nearest eligible untaken 60-minute target
    assert setup["target_found"] is True
    assert setup["target_price"] == B + 18          # the 08:55 swing high
    assert setup["target_distance_r"] == pytest.approx(9.0 / 8.0)

    # 4. entry, stop, fill sequencing
    assert out.trades.height == 1
    t = out.trades.row(0, named=True)
    assert t["order_kind"] == "ORIGINAL" and t["direction"] == "LONG"
    assert t["entry_price"] == B + 9                # proximal edge
    assert t["stop_price"] == B + 1                 # candle 1 low
    assert t["risk_points"] == 8.0
    assert t["order_created_at"].strftime("%H:%M") == "09:33"
    assert t["order_activated_at"].strftime("%H:%M") == "09:34"   # never candle 3
    assert t["filled_at"].strftime("%H:%M") == "09:38"

    # 5. outcome and labels
    assert t["exit_reason"] == "TARGET"
    assert t["result_r"] == pytest.approx(1.125)
    assert t["net_result_r"] < t["result_r"]        # costs always subtract
    assert t["trade_label"] == "CLEAN_WIN"
    assert t["is_clean_win"] is True
    assert t["is_sweaty_win"] is False
    assert t["mae_r"] == pytest.approx(0.0625)
    assert t["mfe_r"] == pytest.approx(1.125)
    assert t["duration_minutes"] == 8
    assert t["ambiguous_execution"] is False


def test_bearish_clean_mirrors_exactly():
    out = _run("bearish_clean")
    setup = out.setups.row(0, named=True)
    assert setup["direction"] == "BEARISH"
    assert setup["fvg_low"] == B - 9 and setup["fvg_high"] == B - 5
    assert setup["proximal_edge"] == B - 9
    assert setup["target_price"] == B - 18          # the 08:55 swing low

    t = out.trades.row(0, named=True)
    assert t["direction"] == "SHORT"
    assert t["entry_price"] == B - 9
    assert t["stop_price"] == B - 1
    assert t["exit_reason"] == "TARGET"
    assert t["result_r"] == pytest.approx(1.125)
    assert t["trade_label"] == "CLEAN_WIN"


# --------------------------------------------------------------------------
# mitigation, inversion, re-inversion
# --------------------------------------------------------------------------


def test_inversion_lifecycle():
    out = _run("bullish_inversion")
    assert out.trades.height == 2
    original, inversion = out.trades.to_dicts()

    # the original long is stopped
    assert original["order_kind"] == "ORIGINAL"
    assert original["exit_reason"] == "STOP"
    assert original["result_r"] == pytest.approx(-1.0)

    # the zone inverted on a completed close below fvg_low, then traded short
    assert inversion["order_kind"] == "INVERSION"
    assert inversion["direction"] == "SHORT"
    assert inversion["entry_price"] == B + 5           # the opposite edge
    assert inversion["stop_price"] == B + 10           # fvg_high + 4 ticks
    # the *nearest* untaken low wins: the 09:30 candle's low at B-0.5, not
    # the older and far more distant 09:05 swing low at B-15
    assert inversion["target_price"] == B - 0.5
    assert inversion["target_age_minutes"] == 9        # 09:30 pivot, 09:39 fill
    assert inversion["exit_reason"] == "TARGET"
    assert inversion["result_r"] == pytest.approx(1.1)

    # the inversion order could not fill on its confirming candle
    events = out.events.filter(pl.col("event") == "INVERSION")
    assert events.height == 1
    inversion_time = events["timestamp"][0]
    assert inversion["order_activated_at"] > inversion_time
    assert inversion["filled_at"] > inversion_time

    # mitigation was recorded but never invalidated the zone
    assert (out.events["event"] == "MITIGATION").sum() > 0
    assert out.setups["zone_touch_count"][0] > 0


def test_zone_survives_mitigation_without_inverting():
    out = _run("bullish_clean")
    setup = out.setups.row(0, named=True)
    assert setup["zone_touch_count"] > 0          # price traded into the zone
    assert setup["zone_inversion_count"] == 0     # yet it never flipped
    assert setup["zone_state_final"] == "ORIGINAL_BULLISH"

    # the ranging session touches it far more often and only inverts once a
    # candle finally closes clean through the far edge
    ranging = _run("ranging")
    rsetup = ranging.setups.row(0, named=True)
    assert rsetup["zone_touch_count"] > setup["zone_touch_count"]
    inversions = ranging.events.filter(pl.col("event") == "INVERSION")
    assert inversions.height == 1
    assert inversions["price"][0] < rsetup["fvg_low"]   # a close beyond the edge


def test_ranging_session_is_labelled():
    out = _run("ranging")
    t = out.trades.row(0, named=True)
    assert t["is_ranging"] is True
    assert t["entry_crossings"] >= 3
    assert t["range_conditions_met"] >= 2
    assert t["exit_reason"] == "STOP"


def test_sweaty_win_is_labelled():
    out = _run("sweaty_win")
    t = out.trades.row(0, named=True)
    assert t["exit_reason"] == "TARGET"
    assert t["is_sweaty_win"] is True
    assert t["is_clean_win"] is False
    assert t["sweaty_conditions_met"] >= 2
    assert t["mae_r"] > 0.5
    assert t["duration_minutes"] > 20
    assert t["inversion_while_open"] is True   # the zone flipped mid-trade


def test_session_without_a_significant_fvg():
    out = _run("no_fvg")
    assert out.setups.is_empty()
    assert out.trades.is_empty()
    assert out.summary["qualifying_fvgs"] == 0
    assert out.summary["sessions_processed"] == 1


# --------------------------------------------------------------------------
# configuration actually changes outcomes
# --------------------------------------------------------------------------


def test_entry_model_changes_the_fill():
    proximal = _run("bullish_clean").trades.row(0, named=True)
    assert proximal["entry_price"] == B + 9

    # the midpoint sits deeper in the zone; this session never trades back
    # that far, so the order simply expires unfilled
    deep = _run("bullish_clean", entries__model="MIDPOINT")
    assert deep.trades.is_empty()
    assert deep.summary["qualifying_fvgs"] == 1
    cancels = deep.events.filter(pl.col("event") == "ORDER_CANCELLED")
    assert cancels.height == 1
    # price ran to the target without ever filling, so the order is pulled
    assert cancels["reason"][0] == "TARGET_SWEPT"

    # a session that does return to the midpoint fills at the deeper price
    filled = _run("ranging", entries__model="MIDPOINT").trades.row(0, named=True)
    assert filled["entry_price"] == B + 7
    assert filled["risk_points"] == 6.0              # candle-1 low is unchanged


def test_thresholds_can_reject_the_setup():
    out = _run("bullish_clean", significance__type_a__minimum_gap_atr=99.0,
               significance__type_b__minimum_fvg_overlap_ratio=99.0)
    assert out.summary["qualifying_fvgs"] == 0
    assert out.setups.height == 1                    # the candidate is still recorded
    assert out.setups["rejection_reason"][0] == "NOT_SIGNIFICANT"
    assert out.trades.is_empty()


def test_inversion_stop_models_are_not_pooled():
    a = _run("bullish_inversion").trades
    b = _run(
        "bullish_inversion",
        inversion__stop_model="INVERSION_CANDLE_EXTREME_PLUS_BUFFER",
    ).trades
    inv_a = a.filter(pl.col("order_kind") == "INVERSION").row(0, named=True)
    inv_b = b.filter(pl.col("order_kind") == "INVERSION").row(0, named=True)
    assert inv_a["inversion_stop_model"] == "OPPOSITE_FVG_EDGE_PLUS_BUFFER"
    assert inv_b["inversion_stop_model"] == "INVERSION_CANDLE_EXTREME_PLUS_BUFFER"
    assert inv_a["stop_price"] != inv_b["stop_price"]
    assert inv_a["risk_points"] != inv_b["risk_points"]


def test_nq_and_mnq_differ_in_money_but_not_in_r():
    nq = _run("bullish_clean", "NQ").trades.row(0, named=True)
    mnq = _run("bullish_clean", "MNQ").trades.row(0, named=True)
    assert nq["result_r"] == pytest.approx(mnq["result_r"])       # same geometry
    assert nq["gross_dollars"] == pytest.approx(10 * mnq["gross_dollars"])
    # the micro's fees are a bigger share of the same move
    assert (nq["result_r"] - nq["net_result_r"]) < (mnq["result_r"] - mnq["net_result_r"])


def test_one_second_execution_mode_runs(clock):
    cfg = AppConfig(
        instrument="NQ", contract_mode="DATED", contract="NQH25",
        start=SESSION.isoformat(), end=SESSION.isoformat(),
    )
    cfg.execution.mode = "ONE_SECOND_INTRABAR"
    cfg.data.execution_resolution = "1s"
    session_clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    provider = SyntheticProvider(
        session_clock, cfg.instruments, schedule={d: "bullish_clean" for d in LEAD_IN_DAYS}
    )
    from datetime import datetime, timedelta, timezone

    start = session_clock.ny_datetime(SESSION, "09:00").astimezone(timezone.utc)
    end = session_clock.ny_datetime(SESSION, "11:00").astimezone(timezone.utc)
    finer = provider.get_bars("NQH25", start, end, "1s", "DATED")
    out = run_backtest(cfg, provider=provider, finer_bars=finer)
    t = out.trades.row(0, named=True)
    assert t["exit_reason"] == "TARGET"
    assert t["result_r"] == pytest.approx(1.125)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def test_run_directory_is_written(tmp_path):
    out = _run("bullish_inversion")
    base = out.write(tmp_path)
    for name in (
        "config.yaml", "contracts.parquet", "setups.parquet", "trades.parquet",
        "events.parquet", "daily_results.parquet", "summary.json", "report.html",
    ):
        assert (base / name).exists(), name

    summary = json.loads((base / "summary.json").read_text())
    assert summary["sessions_processed"] == 1
    assert summary["trades"] == 2
    assert summary["instrument"] == "NQ"

    trades = pl.read_parquet(base / "trades.parquet")
    assert trades.height == 2
    assert "underlying_contract" in trades.columns
    assert trades["underlying_contract"].unique().to_list() == ["NQH25"]

    # the config is reproducible from disk
    from fvg_backtest.config import load_config

    reloaded = load_config(base / "config.yaml")
    assert reloaded.instrument == "NQ"
    assert reloaded.contract == "NQH25"

    events = pl.read_parquet(base / "events.parquet")
    kinds = set(events["event"].to_list())
    assert {"CANDIDATE_DETECTED", "FVG_SELECTED", "ENTRY_ACTIVATION",
            "ENTRY_FILL", "MITIGATION", "INVERSION"} <= kinds

    report = (base / "report.html").read_text()
    assert "First Presented FVG" in report
    assert "no orders were placed" in report


def test_events_include_rejections_and_daily_results():
    out = _run("bullish_inversion")
    assert not out.daily.is_empty()
    day = out.daily.row(0, named=True)
    assert day["trades"] == 2
    assert "cumulative_r" in out.daily.columns
    assert "drawdown_r" in out.daily.columns
    assert out.contracts["underlying_contract"][0] == "NQH25"
