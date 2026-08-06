from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from fvg_backtest.data import normalize_candles, validate_candles
from fvg_backtest.data.synthetic import generate_session_bars

UTC = timezone.utc


@pytest.fixture()
def session_df(clock, jan6):
    raw = generate_session_bars(jan6, "bullish_clean", clock)
    return normalize_candles(
        raw, clock=clock, symbol="NQ", root_symbol="NQ", source="synthetic", resolution="1m"
    )


def test_normalized_schema_columns(session_df):
    from fvg_backtest.data.schema import CANDLE_COLUMNS

    assert session_df.columns == CANDLE_COLUMNS
    assert session_df["timestamp_utc"].dtype.time_zone == "UTC"
    assert session_df["timestamp_ny"].dtype.time_zone == "America/New_York"
    assert session_df["underlying_contract"].null_count() == 0
    assert (session_df["underlying_contract"] == "NQH25").all()


def test_globex_session_date_column(session_df, jan6):
    # bars from Sunday 18:00 through Monday 16:59 all belong to Monday Jan 6
    assert session_df["globex_session_date"].unique().to_list() == [jan6]
    # trading_date differs before midnight (Sunday) vs after (Monday)
    dates = session_df["trading_date"].unique().sort().to_list()
    assert dates == [date(2025, 1, 5), jan6]


def test_vectorized_session_date_matches_the_clock_every_hour_of_a_week(clock):
    """Regression: dt.hour() is Int8, so hour*60 wraps unless widened.

    The weekend correction used to mask it (Sunday-evening bars landed on
    Monday anyway), while every weekday evening bar was silently filed
    under the previous session.
    """
    ny = clock.tz
    stamps = [
        datetime(2025, 1, 6, tzinfo=ny) + timedelta(minutes=30 * i)
        for i in range(24 * 2 * 8)  # a full week and a day, every 30 minutes
    ]
    df = pl.DataFrame(
        {
            "timestamp_utc": [t.astimezone(UTC) for t in stamps],
            "open": [1.0] * len(stamps), "high": [1.0] * len(stamps),
            "low": [1.0] * len(stamps), "close": [1.0] * len(stamps),
        }
    )
    got = normalize_candles(
        df, clock=clock, symbol="NQ", root_symbol="NQ", source="csv",
        resolution="1m", underlying_contract="NQH25",
    )
    expected = [clock.globex_session_date(t) for t in got["timestamp_utc"]]
    assert got["globex_session_date"].to_list() == expected

    # and specifically: Monday 2025-01-06 18:00 belongs to Tuesday's session
    monday_evening = got.filter(
        pl.col("timestamp_ny").dt.date() == date(2025, 1, 6)
    ).filter(pl.col("timestamp_ny").dt.hour() >= 18)
    assert monday_evening["globex_session_date"].unique().to_list() == [date(2025, 1, 7)]


def test_mandatory_columns_enforced(clock):
    with pytest.raises(ValueError, match="missing required"):
        normalize_candles(
            pl.DataFrame({"timestamp_utc": [datetime(2025, 1, 6, tzinfo=UTC)], "open": [1.0]}),
            clock=clock, symbol="NQ", root_symbol="NQ", source="csv", resolution="1m",
        )


def test_contract_never_implicit(clock):
    df = pl.DataFrame(
        {
            "timestamp_utc": [datetime(2025, 1, 6, 14, 30, tzinfo=UTC)],
            "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
        }
    )
    with pytest.raises(ValueError, match="underlying_contract"):
        normalize_candles(df, clock=clock, symbol="NQ", root_symbol="NQ", source="csv", resolution="1m")


def test_quality_clean_session_has_no_errors(session_df, clock):
    report = validate_candles(session_df, clock)
    assert report.ok, [i.message for i in report.errors]
    assert report.sessions == 1


def test_quality_detects_duplicates(session_df, clock):
    dup = pl.concat([session_df, session_df.tail(1)])
    report = validate_candles(dup.sort("timestamp_utc"), clock)
    assert any(i.check == "duplicate_timestamps" for i in report.errors)


def test_quality_detects_invalid_ohlc(session_df, clock):
    # high dropped below the low on one bar
    bad = session_df.with_columns(
        pl.when(pl.arange(0, pl.len()) == 5)
        .then(pl.col("low") - 5.0)
        .otherwise(pl.col("high"))
        .alias("high")
    )
    report = validate_candles(bad, clock)
    assert any(i.check == "invalid_ohlc" for i in report.errors)


def test_quality_detects_out_of_order(session_df, clock):
    shuffled = pl.concat([session_df.tail(10), session_df.head(200)])
    report = validate_candles(shuffled, clock)
    assert any(i.check == "out_of_order" for i in report.errors)


def test_quality_detects_gaps(session_df, clock):
    with_gap = pl.concat([session_df.head(400), session_df.tail(session_df.height - 430)])
    report = validate_candles(with_gap, clock)
    checks = {i.check for i in report.issues}
    assert "missing_bars" in checks
    assert "abnormally_long_gaps" in checks


def test_quality_detects_mixed_contracts_in_session(session_df, clock):
    mixed = session_df.with_columns(
        pl.when(pl.arange(0, pl.len()) > 700)
        .then(pl.lit("NQM25"))
        .otherwise(pl.col("underlying_contract"))
        .alias("underlying_contract")
    )
    report = validate_candles(mixed, clock)
    assert any(i.check == "contract_change_mid_session" for i in report.errors)


def test_quality_holiday_bars_flagged(clock):
    # bars on Jan 1 (holiday) get a warning
    raw = generate_session_bars(date(2025, 1, 1), "no_fvg", clock)
    df = normalize_candles(
        raw, clock=clock, symbol="NQ", root_symbol="NQ", source="synthetic", resolution="1m"
    )
    report = validate_candles(df, clock)
    assert any(i.check == "holiday_sessions" for i in report.issues)


def test_quality_timezone_heuristic(session_df, clock):
    # mislabel: shift timestamps 7 hours so cash volume lands overnight
    shifted = session_df.with_columns(
        (pl.col("timestamp_utc") + timedelta(hours=12)).alias("timestamp_utc")
    ).drop("timestamp_ny", "trading_date", "globex_session_date")
    renorm = normalize_candles(
        shifted, clock=clock, symbol="NQ", root_symbol="NQ", source="csv", resolution="1m"
    )
    report = validate_candles(renorm, clock)
    assert any(i.check == "timezone_suspicious" for i in report.issues)
