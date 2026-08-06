"""Hand-built one-minute bar frames for exact rule tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from fvg_backtest.data.schema import normalize_candles
from fvg_backtest.features.indicators import add_indicators

NY = ZoneInfo("America/New_York")


def bars_from(
    clock,
    session_date: date,
    start_hhmm: str,
    ohlc: list[tuple[float, float, float, float]],
    *,
    volume: float = 500.0,
    contract: str = "NQH25",
    atr_length: int = 20,
) -> pl.DataFrame:
    """Build a normalized + indicator-decorated frame from raw OHLC tuples.

    ``ohlc`` is a list of (open, high, low, close) starting at ``start_hhmm``
    New York on ``session_date``, one bar per minute.
    """
    h, m = (int(x) for x in start_hhmm.split(":"))
    t0 = datetime(session_date.year, session_date.month, session_date.day, h, m, tzinfo=NY)
    rows = []
    for i, (o, hi, lo, c) in enumerate(ohlc):
        rows.append(
            {
                "timestamp_utc": (t0 + timedelta(minutes=i)).astimezone(ZoneInfo("UTC")),
                "open": float(o), "high": float(hi), "low": float(lo), "close": float(c),
                "volume": volume,
                "trade_count": 10,
                "vwap": float(c),
                "underlying_contract": contract,
            }
        )
    df = normalize_candles(
        pl.DataFrame(rows), clock=clock, symbol="NQ", root_symbol="NQ",
        source="synthetic", resolution="1m",
    )
    return add_indicators(df, atr_length=atr_length)


def flat_bars(n: int, price: float = 21000.0, rng: float = 1.0):
    """``n`` identical doji-ish candles: they overlap, so no FVG can form
    among them, and their wicks are long enough to satisfy Type B."""
    return [(price, price + rng, price - rng, price) for _ in range(n)]


def body_bars(n: int, price: float = 21000.0, body: float = 1.0, wick: float = 0.05):
    """``n`` alternating full-body candles with negligible wicks.

    Used as a lead-in when a test needs Type B to *fail*: wick share is
    ``wick / (body + 2*wick)``, far below the 0.40 threshold.
    """
    half = body / 2
    up = (price - half, price + half + wick, price - half - wick, price + half)
    down = (price + half, price + half + wick, price - half - wick, price - half)
    return [up if i % 2 == 0 else down for i in range(n)]
