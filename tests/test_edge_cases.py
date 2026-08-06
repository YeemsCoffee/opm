"""Edge cases the spec calls out explicitly.

Daylight saving, early closes, missing bars, contract rolls during an open
position, and the intrabar execution modes are all easy to get subtly wrong,
so each gets a test that would fail loudly if the behaviour regressed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from fvg_backtest.config import AppConfig
from fvg_backtest.data.schema import normalize_candles
from fvg_backtest.data.synthetic import SyntheticProvider, generate_session_bars
from fvg_backtest.execution.simulator import TradeSimulator
from fvg_backtest.features.indicators import add_indicators
from fvg_backtest.pipeline import run_backtest
from fvg_backtest.sessions import SessionClock, TradingCalendar

UTC = timezone.utc
B = 21000.0
LEAD_IN = [date(2025, 1, 2), date(2025, 1, 3)]


def _provider(cfg, clock, scenario, days):
    return SyntheticProvider(
        clock, cfg.instruments, schedule={d: scenario for d in days}
    )


def _cfg(**kw) -> AppConfig:
    base = dict(
        instrument="NQ", contract_mode="DATED", contract="NQH25",
        start="2025-01-06", end="2025-01-06",
    )
    base.update(kw)
    return AppConfig(**base)


# --------------------------------------------------------------------------
# daylight saving
# --------------------------------------------------------------------------


def test_backtest_across_the_spring_dst_transition(clock):
    """2025-03-09 loses an hour; sessions either side must still work."""
    cfg = _cfg(start="2025-03-07", end="2025-03-12")
    session_clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    days = session_clock.calendar.trading_days(date(2025, 3, 3), date(2025, 3, 12))
    out = run_backtest(
        cfg, provider=_provider(cfg, session_clock, "bullish_clean", days), keep_bars=True
    )
    assert out.summary["sessions_processed"] == 4        # Mar 7, 10, 11, 12
    assert out.trades.height == 4

    # 09:30 New York is a fixed wall clock, but a moving UTC hour
    bars = out.bars
    opens = (
        bars.filter(
            (pl.col("timestamp_ny").dt.hour() == 9) & (pl.col("timestamp_ny").dt.minute() == 30)
        )
        .with_columns(pl.col("timestamp_utc").dt.hour().alias("utc_hour"))
        .select("globex_session_date", "utc_hour")
        .unique()
        .sort("globex_session_date")
    )
    by_date = dict(opens.iter_rows())
    assert by_date[date(2025, 3, 7)] == 14               # EST
    assert by_date[date(2025, 3, 10)] == 13              # EDT
    # every session still detects its FVG at the same wall-clock minute
    assert set(out.setups["c3_time"].dt.strftime("%H:%M").to_list()) == {"09:33"}


def test_backtest_across_the_autumn_dst_transition():
    cfg = _cfg(start="2025-10-31", end="2025-11-04")
    session_clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    days = session_clock.calendar.trading_days(date(2025, 10, 27), date(2025, 11, 4))
    out = run_backtest(cfg, provider=_provider(cfg, session_clock, "bearish_clean", days))
    assert out.summary["sessions_processed"] == 3        # Oct 31, Nov 3, Nov 4
    assert set(out.setups["c3_time"].dt.strftime("%H:%M").to_list()) == {"09:33"}


# --------------------------------------------------------------------------
# early close
# --------------------------------------------------------------------------


def test_early_close_shortens_the_management_window(clock):
    early = date(2025, 11, 28)                            # day after Thanksgiving
    assert clock.calendar.is_early_close(early)
    assert clock.management_end_dt(early).strftime("%H:%M") == "13:00"
    assert clock.fvg_search_end_dt(early).strftime("%H:%M") == "13:00"
    # a normal session still runs to 16:00
    assert clock.management_end_dt(date(2025, 11, 26)).strftime("%H:%M") == "16:00"


def test_position_is_closed_at_an_early_close(clock, config):
    """A trade still open at 13:00 on an early-close day is forced out."""
    early = date(2025, 11, 28)
    cfg = _cfg(start=early.isoformat(), end=early.isoformat())
    session_clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    days = [date(2025, 11, 24), date(2025, 11, 25), early]
    out = run_backtest(cfg, provider=_provider(cfg, session_clock, "ranging", days))
    assert out.summary["sessions_processed"] == 1
    if not out.trades.is_empty():
        last_exit = out.trades["exit_time"].max()
        assert last_exit <= session_clock.management_end_dt(early)


# --------------------------------------------------------------------------
# missing bars
# --------------------------------------------------------------------------


def test_data_gap_cancels_a_pending_order(clock, config):
    """A hole in the tape means sequencing cannot be trusted."""
    session = date(2025, 1, 6)
    raw = generate_session_bars(session, "bullish_clean", clock)
    df = normalize_candles(
        raw, clock=clock, symbol="NQ", root_symbol="NQ", source="synthetic", resolution="1m"
    )
    # remove 09:35–09:37, right before the 09:38 fill
    holed = df.filter(
        ~(
            (pl.col("timestamp_ny").dt.hour() == 9)
            & (pl.col("timestamp_ny").dt.minute().is_between(35, 37))
        )
    )
    bars = add_indicators(holed, config.atr.length, config.atr.method)
    result = TradeSimulator(config, clock).run_session(bars, session)

    assert result.selected is not None
    assert any("data gap" in w for w in result.warnings)
    cancels = [e for e in result.events if e["event"] == "ORDER_CANCELLED"]
    assert any(e.get("reason") == "DATA_INCOMPLETE" for e in cancels)
    assert result.trades == []          # no fill on untrustworthy data


def test_quality_report_flags_the_same_gap(clock, config):
    from fvg_backtest.data.quality import validate_candles

    raw = generate_session_bars(date(2025, 1, 6), "bullish_clean", clock)
    df = normalize_candles(
        raw, clock=clock, symbol="NQ", root_symbol="NQ", source="synthetic", resolution="1m"
    )
    holed = df.filter(
        ~(
            (pl.col("timestamp_ny").dt.hour() == 9)
            & (pl.col("timestamp_ny").dt.minute().is_between(35, 37))
        )
    )
    report = validate_candles(holed, clock)
    assert any(i.check == "missing_bars" for i in report.issues)


# --------------------------------------------------------------------------
# contract rolls
# --------------------------------------------------------------------------


def test_contract_change_mid_session_closes_everything(clock, config):
    """No trade may survive a contract change."""
    session = date(2025, 1, 6)
    raw = generate_session_bars(session, "ranging", clock)
    df = normalize_candles(
        raw, clock=clock, symbol="NQ", root_symbol="NQ", source="synthetic", resolution="1m"
    )
    # swap the contract from 10:00 onwards, while the ranging trade is open
    switched = df.with_columns(
        pl.when(
            (pl.col("timestamp_ny").dt.hour() >= 10)
            & (pl.col("timestamp_ny").dt.hour() < 17)
        )
        .then(pl.lit("NQM25"))
        .otherwise(pl.col("underlying_contract"))
        .alias("underlying_contract")
    )
    bars = add_indicators(switched, config.atr.length, config.atr.method)
    result = TradeSimulator(config, clock).run_session(bars, session)

    assert any("contract changed" in w for w in result.warnings)
    assert any(e["event"] == "CONTRACT_ROLL" for e in result.events)
    assert result.trades, "the open position should have been closed out"
    trade = result.trades[-1]
    assert trade.exit_reason == "SESSION_CLOSE"
    assert trade.exit_time.strftime("%H:%M") == "10:00"


def test_rollover_sessions_can_be_excluded_from_a_run():
    # a range spanning normal sessions *and* the March roll
    cfg = _cfg(contract_mode="CONTINUOUS", contract=None,
               start="2025-02-24", end="2025-03-20")
    session_clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    days = session_clock.calendar.trading_days(date(2025, 2, 18), date(2025, 3, 20))
    provider = _provider(cfg, session_clock, "bullish_clean", days)

    full = run_backtest(cfg.model_copy(deep=True), provider=provider)
    periods = set(full.contracts["roll_period"].to_list())
    assert {"NORMAL", "ROLLOVER_TRANSITION"} <= periods

    trimmed_cfg = cfg.model_copy(deep=True)
    trimmed_cfg.rolls.exclude_rollover_sessions = True
    trimmed = run_backtest(trimmed_cfg, provider=provider)
    assert "ROLLOVER_TRANSITION" not in trimmed.contracts["roll_period"].to_list()
    assert trimmed.summary["sessions_processed"] < full.summary["sessions_processed"]


def test_excluding_every_session_is_an_explicit_error():
    """Filtering the whole range away must say so, not return an empty run."""
    cfg = _cfg(start="2025-03-17", end="2025-03-20")   # NQH25 expiration week
    cfg.rolls.exclude_rollover_sessions = True
    cfg.rolls.exclude_expiration_week = True
    session_clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    days = session_clock.calendar.trading_days(date(2025, 3, 11), date(2025, 3, 20))
    provider = _provider(cfg, session_clock, "bullish_clean", days)
    with pytest.raises(ValueError, match="removed every session"):
        run_backtest(cfg, provider=provider)


# --------------------------------------------------------------------------
# intrabar execution modes
# --------------------------------------------------------------------------


def _finer(cfg, session_clock, scenario, days, resolution):
    provider = _provider(cfg, session_clock, scenario, days)
    start = session_clock.ny_datetime(date.fromisoformat(cfg.start), "09:00").astimezone(UTC)
    end = session_clock.ny_datetime(date.fromisoformat(cfg.end), "16:00").astimezone(UTC)
    return provider, provider.get_bars(cfg.contract, start, end, resolution, "DATED")


def test_one_second_and_conservative_agree_on_an_unambiguous_session():
    days = LEAD_IN + [date(2025, 1, 6)]
    base = _cfg()
    clock = SessionClock(config=base.sessions, calendar=TradingCalendar())
    provider, seconds = _finer(base, clock, "bullish_clean", days, "1s")

    conservative = run_backtest(base, provider=provider).trades.row(0, named=True)

    intrabar_cfg = _cfg()
    intrabar_cfg.execution.mode = "ONE_SECOND_INTRABAR"
    intrabar = run_backtest(
        intrabar_cfg, provider=provider, finer_bars=seconds
    ).trades.row(0, named=True)

    assert conservative["exit_reason"] == intrabar["exit_reason"] == "TARGET"
    assert conservative["result_r"] == pytest.approx(intrabar["result_r"])
    assert intrabar["ambiguous_execution"] is False


def test_tick_mode_runs_end_to_end():
    days = LEAD_IN + [date(2025, 1, 6)]
    cfg = _cfg()
    cfg.execution.mode = "TICK_INTRABAR"
    clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    provider = _provider(cfg, clock, "bullish_inversion", days)
    start = clock.ny_datetime(date(2025, 1, 6), "09:00").astimezone(UTC)
    end = clock.ny_datetime(date(2025, 1, 6), "16:00").astimezone(UTC)
    ticks = provider.get_bars("NQH25", start, end, "tick", "DATED")
    assert ticks.columns == ["timestamp_utc", "price", "size", "underlying_contract"]

    ticks = ticks.with_columns(
        pl.col("timestamp_utc").dt.convert_time_zone("America/New_York").alias("timestamp_ny"),
        pl.lit(date(2025, 1, 6)).alias("globex_session_date"),
    )
    out = run_backtest(cfg, provider=provider, finer_bars=ticks)
    assert out.trades.height == 2
    assert out.trades["exit_reason"].to_list() == ["STOP", "TARGET"]


def test_ambiguity_is_logged_when_a_minute_spans_stop_and_target(clock, config):
    """A single candle covering entry, stop and target resolves adversely."""
    session = date(2025, 1, 6)
    raw = generate_session_bars(session, "bullish_clean", clock)
    df = normalize_candles(
        raw, clock=clock, symbol="NQ", root_symbol="NQ", source="synthetic", resolution="1m"
    )
    # widen the 09:38 fill candle so it reaches the stop (B+1) and the
    # target (B+18) in the same minute
    wild = df.with_columns(
        pl.when(
            (pl.col("timestamp_ny").dt.hour() == 9) & (pl.col("timestamp_ny").dt.minute() == 38)
        ).then(pl.lit(B + 19)).otherwise(pl.col("high")).alias("high"),
        pl.when(
            (pl.col("timestamp_ny").dt.hour() == 9) & (pl.col("timestamp_ny").dt.minute() == 38)
        ).then(pl.lit(B + 0.5)).otherwise(pl.col("low")).alias("low"),
    )
    bars = add_indicators(wild, config.atr.length, config.atr.method)
    result = TradeSimulator(config, clock).run_session(bars, session)

    assert any(e["event"] == "AMBIGUOUS_SEQUENCE" for e in result.events)
    trade = result.trades[0]
    assert trade.exit_reason == "STOP"          # adverse assumption wins
    assert trade.ambiguous_events >= 1
