"""Normalized candle schema.

Every provider (Databento, CSV, Parquet, synthetic) funnels through
:func:`normalize_candles`, so the rest of the system sees one schema:

======================  =======================================================
timestamp_utc           bar open time, tz-aware UTC (mandatory)
timestamp_ny            bar open time converted to America/New_York
trading_date            NY calendar date of the bar
globex_session_date     cash date whose Globex session owns the bar
symbol                  the symbol as requested (NQ, NQH25, NQ.c.0 …)
root_symbol             NQ | MNQ
underlying_contract     actual dated contract for the bar (never blank)
open/high/low/close     prices (mandatory)
volume                  contracts traded (0 when unknown)
trade_count             number of trades (null when unknown)
vwap                    volume-weighted average price (null when unknown)
source                  databento | csv | parquet | synthetic
resolution              1m | 1s | tick
======================  =======================================================

Only timestamp and OHLC are mandatory inputs; everything else is derived or
defaulted.  Different underlying contracts are never silently combined —
``underlying_contract`` is carried on every row and validated downstream.
"""

from __future__ import annotations

import polars as pl

from ..sessions.clock import SessionClock, hhmm_to_minutes

REQUIRED_COLUMNS = ["timestamp_utc", "open", "high", "low", "close"]

CANDLE_COLUMNS = [
    "timestamp_utc",
    "timestamp_ny",
    "trading_date",
    "globex_session_date",
    "symbol",
    "root_symbol",
    "underlying_contract",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
    "source",
    "resolution",
]


def _ensure_utc(df: pl.DataFrame, column: str) -> pl.DataFrame:
    dtype = df.schema[column]
    if dtype == pl.Datetime:
        tz = dtype.time_zone  # type: ignore[union-attr]
        if tz is None:
            # tz-naive input is assumed to already be UTC
            df = df.with_columns(pl.col(column).dt.replace_time_zone("UTC"))
        elif tz != "UTC":
            df = df.with_columns(pl.col(column).dt.convert_time_zone("UTC"))
    elif dtype == pl.String:
        df = df.with_columns(
            pl.col(column).str.to_datetime(time_zone="UTC", time_unit="us")
        )
    else:
        raise TypeError(f"{column} must be datetime or ISO string, got {dtype}")
    return df.with_columns(pl.col(column).dt.cast_time_unit("us"))


def _globex_session_date(tz: str, start_hhmm: str) -> pl.Expr:
    """Vectorized session-date assignment: >= 18:00 rolls to the next day,
    weekend results shift to Monday (Sunday-evening bars belong to Monday)."""
    ny = pl.col("timestamp_ny")
    start_min = hhmm_to_minutes(start_hhmm)
    # dt.hour() is Int8: multiplying by 60 in that width silently wraps
    # (18*60 = 1080 -> 56), so widen before doing arithmetic on it
    minute_of_day = ny.dt.hour().cast(pl.Int32) * 60 + ny.dt.minute().cast(pl.Int32)
    base = ny.dt.date() + pl.when(minute_of_day >= start_min).then(
        pl.duration(days=1)
    ).otherwise(pl.duration(days=0))
    wd = base.dt.weekday()  # Monday=1 .. Sunday=7
    return (
        base
        + pl.when(wd == 6)
        .then(pl.duration(days=2))
        .when(wd == 7)
        .then(pl.duration(days=1))
        .otherwise(pl.duration(days=0))
    ).cast(pl.Date)


def normalize_candles(
    df: pl.DataFrame,
    *,
    clock: SessionClock,
    symbol: str,
    root_symbol: str,
    source: str,
    resolution: str,
    underlying_contract: str | None = None,
) -> pl.DataFrame:
    """Normalize provider output to the canonical candle schema.

    ``underlying_contract`` may be a constant (dated-contract requests) or
    already present as a column (continuous series);  it must end up
    non-null on every row.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"candle data missing required columns: {missing}")

    df = _ensure_utc(df, "timestamp_utc")
    tz = clock.config.timezone
    df = df.with_columns(
        pl.col("timestamp_ny").dt.convert_time_zone(tz).dt.cast_time_unit("us")
        if "timestamp_ny" in df.columns
        else pl.col("timestamp_utc").dt.convert_time_zone(tz).alias("timestamp_ny")
    )
    if "underlying_contract" not in df.columns:
        if not underlying_contract:
            raise ValueError(
                "underlying_contract required (column or constant) — "
                "contracts are never implicit"
            )
        df = df.with_columns(pl.lit(underlying_contract).alias("underlying_contract"))
    elif underlying_contract:
        df = df.with_columns(
            pl.col("underlying_contract").fill_null(underlying_contract)
        )
    if df["underlying_contract"].null_count() > 0:
        raise ValueError("underlying_contract has null rows")

    defaults = []
    if "volume" not in df.columns:
        defaults.append(pl.lit(0.0).alias("volume"))
    if "trade_count" not in df.columns:
        defaults.append(pl.lit(None, dtype=pl.Int64).alias("trade_count"))
    if "vwap" not in df.columns:
        defaults.append(pl.lit(None, dtype=pl.Float64).alias("vwap"))
    df = df.with_columns(defaults) if defaults else df

    df = df.with_columns(
        pl.col("timestamp_ny").dt.date().alias("trading_date"),
        _globex_session_date(tz, clock.config.globex_session_start).alias(
            "globex_session_date"
        ),
        pl.lit(symbol).alias("symbol"),
        pl.lit(root_symbol).alias("root_symbol"),
        pl.lit(source).alias("source"),
        pl.lit(resolution).alias("resolution"),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
    )
    return df.select(CANDLE_COLUMNS).sort("timestamp_utc")
