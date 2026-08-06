from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from fvg_backtest.data.schema import normalize_candles
from fvg_backtest.futures.series import build_continuous_series, daily_volumes_by_contract

UTC = timezone.utc


def _two_contract_frame(clock, days: int = 60, start: date = date(2025, 1, 20)):
    """Both NQH25 and NQM25 quoted every session, NQM25 100 points higher and
    taking over volume leadership on 2025-03-13."""
    rows = []
    d = start
    n = 0
    while n < days:
        if clock.calendar.is_trading_day(d):
            open_ny = clock.cash_open_dt(d)
            for m in range(30):
                ts = (open_ny + timedelta(minutes=m)).astimezone(UTC)
                for sym, base in (("NQH25", 21000.0), ("NQM25", 21100.0)):
                    lead = sym == ("NQM25" if d >= date(2025, 3, 13) else "NQH25")
                    rows.append(
                        {
                            "timestamp_utc": ts,
                            "open": base + m, "high": base + m + 2,
                            "low": base + m - 2, "close": base + m + 1,
                            "volume": 900.0 if lead else 100.0,
                            "underlying_contract": sym,
                        }
                    )
            n += 1
        d += timedelta(days=1)
    return normalize_candles(
        pl.DataFrame(rows), clock=clock, symbol="NQ", root_symbol="NQ",
        source="synthetic", resolution="1m",
    )


def test_fixed_days_series_has_one_contract_per_session(clock, config):
    df = _two_contract_frame(clock)
    cfg = config.rolls.model_copy(update={"method": "FIXED_DAYS_BEFORE_EXPIRATION"})
    series, schedule = build_continuous_series(df, config.instruments["NQ"], cfg)
    per_session = (
        series.group_by("globex_session_date")
        .agg(pl.col("underlying_contract").n_unique().alias("n"))
    )
    assert per_session["n"].max() == 1  # contracts are never blended
    contracts = series["underlying_contract"].unique().to_list()
    assert set(contracts) == {"NQH25", "NQM25"}


def test_roll_annotations_present(clock, config):
    df = _two_contract_frame(clock)
    cfg = config.rolls.model_copy(update={"method": "FIXED_DAYS_BEFORE_EXPIRATION"})
    series, _ = build_continuous_series(df, config.instruments["NQ"], cfg)
    for col in ("roll_method", "days_to_expiration", "roll_period", "contract_expiration"):
        assert col in series.columns
    assert series["roll_method"].unique().to_list() == ["FIXED_DAYS_BEFORE_EXPIRATION"]
    mar13 = series.filter(pl.col("globex_session_date") == date(2025, 3, 13))
    assert mar13["underlying_contract"][0] == "NQH25"
    assert mar13["days_to_expiration"][0] == 8
    mar14 = series.filter(pl.col("globex_session_date") == date(2025, 3, 14))
    assert mar14["underlying_contract"][0] == "NQM25"


def test_highest_volume_roll_uses_actual_volume(clock, config):
    df = _two_contract_frame(clock)
    vols = daily_volumes_by_contract(df)
    assert vols["NQH25"][date(2025, 3, 12)] > vols["NQM25"][date(2025, 3, 12)]
    assert vols["NQM25"][date(2025, 3, 13)] > vols["NQH25"][date(2025, 3, 13)]
    cfg = config.rolls.model_copy(update={"method": "HIGHEST_VOLUME"})
    series, _ = build_continuous_series(df, config.instruments["NQ"], cfg)
    front = lambda d: series.filter(pl.col("globex_session_date") == d)["underlying_contract"][0]
    # a day's volume is only known once that day has closed, so the new front
    # takes over on the session *after* the crossing — using it same-day
    # would be lookahead
    assert front(date(2025, 3, 12)) == "NQH25"
    assert front(date(2025, 3, 13)) == "NQH25"
    assert front(date(2025, 3, 14)) == "NQM25"


def test_rollover_sessions_can_be_excluded(clock, config):
    df = _two_contract_frame(clock)
    keep = config.rolls.model_copy(update={"method": "FIXED_DAYS_BEFORE_EXPIRATION"})
    drop = keep.model_copy(update={"exclude_rollover_sessions": True})
    full, _ = build_continuous_series(df, config.instruments["NQ"], keep)
    trimmed, _ = build_continuous_series(df, config.instruments["NQ"], drop)
    assert "ROLLOVER_TRANSITION" in full["roll_period"].unique().to_list()
    assert "ROLLOVER_TRANSITION" not in trimmed["roll_period"].unique().to_list()
    assert trimmed.height < full.height


def test_back_adjust_off_by_default_and_warns_when_on(clock, config):
    df = _two_contract_frame(clock)
    cfg = config.rolls.model_copy(update={"method": "FIXED_DAYS_BEFORE_EXPIRATION"})
    plain, _ = build_continuous_series(df, config.instruments["NQ"], cfg)
    assert "back_adjusted" not in plain.columns
    # prices stay untouched: the H25 leg keeps its real 21000 handle
    h25 = plain.filter(pl.col("underlying_contract") == "NQH25")
    assert h25["open"].min() == 21000.0

    adj_cfg = cfg.model_copy(update={"back_adjust": True})
    with pytest.warns(UserWarning, match="price-level"):
        adjusted, _ = build_continuous_series(df, config.instruments["NQ"], adj_cfg)
    assert adjusted["back_adjusted"][0] is True
    h25_adj = adjusted.filter(pl.col("underlying_contract") == "NQH25")
    assert h25_adj["open"].min() != 21000.0  # shifted by the roll gap
