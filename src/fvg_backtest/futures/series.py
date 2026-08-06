"""Continuous research-series construction.

Given per-contract candles and a roll schedule, keep exactly the front
contract's bars for each session date and tag every row with
``underlying_contract``, ``roll_method``, ``days_to_expiration`` and
``roll_period``.  Contracts are never blended: each session comes wholly
from one contract.

Back-adjustment is available but **off by default** — the strategy trades
actual price levels (gap edges, swing highs/lows, stop distances) and any
additive offset corrupts them.  Enabling it emits a loud warning.
"""

from __future__ import annotations

import warnings
from datetime import date

import polars as pl

from ..config.schema import InstrumentConfig, RollConfig
from .contracts import contract_expiration, parse_contract
from .rolls import RollSegment, build_roll_schedule, classify_roll_period

BACK_ADJUST_WARNING = (
    "back_adjust=True shifts historical prices by roll gaps. FVG boundaries, "
    "swing levels, round numbers and stop distances are all price-level "
    "dependent, so back-adjusted results are NOT comparable to live trading. "
    "Use contract_mode=DATED for execution-grade research."
)


def daily_volumes_by_contract(df: pl.DataFrame) -> dict[str, dict[date, float]]:
    """{contract: {session_date: volume}} — input for HIGHEST_VOLUME rolls."""
    grouped = df.group_by(["underlying_contract", "globex_session_date"]).agg(
        pl.col("volume").sum().alias("v")
    )
    out: dict[str, dict[date, float]] = {}
    for row in grouped.iter_rows(named=True):
        out.setdefault(row["underlying_contract"], {})[row["globex_session_date"]] = row["v"]
    return out


def build_continuous_series(
    df: pl.DataFrame,
    instrument: InstrumentConfig,
    config: RollConfig,
    start: date | None = None,
    end: date | None = None,
) -> tuple[pl.DataFrame, list[RollSegment]]:
    """Select front-contract bars per session and annotate roll context."""
    if df.is_empty():
        raise ValueError("no candles supplied")
    sessions = df["globex_session_date"]
    start = start or sessions.min()
    end = end or sessions.max()

    schedule = build_roll_schedule(
        instrument, start, end, config,
        daily_volumes=daily_volumes_by_contract(df) if config.method == "HIGHEST_VOLUME" else None,
    )
    front = pl.DataFrame(
        [
            {"globex_session_date": d, "_front": seg.contract.code}
            for seg in schedule
            for d in _dates(seg.first_session, seg.last_session)
        ],
        schema={"globex_session_date": pl.Date, "_front": pl.String},
    )
    joined = df.join(front, on="globex_session_date", how="inner")
    out = joined.filter(pl.col("underlying_contract") == pl.col("_front")).drop("_front")
    if out.is_empty():
        raise ValueError(
            "no bars matched the roll schedule — the data may not cover the "
            "front contracts for this range"
        )

    expiries = {
        c: contract_expiration(parse_contract(c), instrument)
        for c in out["underlying_contract"].unique().to_list()
    }
    periods: dict[tuple[date, str], tuple[str, int]] = {}
    for row in out.select("globex_session_date", "underlying_contract").unique().iter_rows():
        d, c = row
        p, dte = classify_roll_period(d, c, instrument, config)
        periods[(d, c)] = (str(p), dte)

    out = out.with_columns(
        pl.lit(config.method).alias("roll_method"),
        pl.struct("globex_session_date", "underlying_contract")
        .map_elements(
            lambda s: periods[(s["globex_session_date"], s["underlying_contract"])][1],
            return_dtype=pl.Int64,
        )
        .alias("days_to_expiration"),
        pl.struct("globex_session_date", "underlying_contract")
        .map_elements(
            lambda s: periods[(s["globex_session_date"], s["underlying_contract"])][0],
            return_dtype=pl.String,
        )
        .alias("roll_period"),
        pl.col("underlying_contract")
        .replace_strict(expiries, return_dtype=pl.Date)
        .alias("contract_expiration"),
    )

    before = out.height
    if config.exclude_rollover_sessions:
        out = out.filter(pl.col("roll_period") != "ROLLOVER_TRANSITION")
    if config.exclude_expiration_week:
        out = out.filter(pl.col("roll_period") != "EXPIRATION_WEEK")
    if before and out.is_empty():
        raise ValueError(
            "the roll-period exclusions removed every session in this range — "
            "widen the dates or turn off exclude_rollover_sessions / "
            "exclude_expiration_week"
        )

    if config.back_adjust:
        warnings.warn(BACK_ADJUST_WARNING, UserWarning, stacklevel=2)
        out = _back_adjust(out)
    return out.sort("timestamp_utc"), schedule


def _dates(a: date, b: date) -> list[date]:
    from datetime import timedelta

    out, d = [], a
    while d <= b:
        out.append(d)
        d += timedelta(days=1)
    return out


def _back_adjust(df: pl.DataFrame) -> pl.DataFrame:
    """Additive back-adjustment: shift older contracts by the roll gap so the
    series is continuous at the joins (see BACK_ADJUST_WARNING)."""
    sessions = (
        df.group_by("underlying_contract")
        .agg(
            pl.col("globex_session_date").min().alias("first"),
            pl.col("globex_session_date").max().alias("last"),
            pl.col("close").sort_by("timestamp_utc").last().alias("last_close"),
            pl.col("close").sort_by("timestamp_utc").first().alias("first_close"),
        )
        .sort("first")
    )
    rows = sessions.to_dicts()
    offsets: dict[str, float] = {rows[-1]["underlying_contract"]: 0.0} if rows else {}
    cumulative = 0.0
    for prev, nxt in zip(reversed(rows[:-1]), reversed(rows[1:])):
        cumulative += nxt["first_close"] - prev["last_close"]
        offsets[prev["underlying_contract"]] = cumulative
    adj = pl.col("underlying_contract").replace_strict(offsets, return_dtype=pl.Float64)
    return df.with_columns(
        [(pl.col(c) + adj).alias(c) for c in ("open", "high", "low", "close")]
        + [pl.lit(True).alias("back_adjusted")]
    )
