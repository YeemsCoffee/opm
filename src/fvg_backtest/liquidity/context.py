"""NQ/MNQ context levels stored as *features*, never as automatic targets.

These describe where the setup sits relative to the session's structure —
prior cash session, Globex, overnight, premarket, the opening ranges and
round numbers — so the analysis can ask whether proximity to them changes
performance.  They never replace the 60-minute liquidity target.

Everything is computed from bars **at or before** the decision time, so no
value here can leak the future.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

import polars as pl

from ..config.schema import ContextConfig
from ..sessions.clock import SessionClock


@dataclass
class ContextLevels:
    prior_cash_high: float | None = None
    prior_cash_low: float | None = None
    prior_cash_close: float | None = None
    globex_high: float | None = None
    globex_low: float | None = None
    overnight_high: float | None = None
    overnight_low: float | None = None
    premarket_high: float | None = None
    premarket_low: float | None = None
    open_price: float | None = None
    opening_range_short_high: float | None = None
    opening_range_short_low: float | None = None
    opening_range_long_high: float | None = None
    opening_range_long_low: float | None = None
    opening_range_midpoint: float | None = None
    opening_range_short_size: float | None = None
    opening_range_long_size: float | None = None
    cash_high_so_far: float | None = None
    cash_low_so_far: float | None = None
    session_midpoint: float | None = None
    developing_range: float | None = None
    nearest_whole_number: float | None = None
    nearest_round_increment: float | None = None
    high_15m: float | None = None
    low_15m: float | None = None
    high_30m: float | None = None
    low_30m: float | None = None
    high_60m: float | None = None
    low_60m: float | None = None

    def to_dict(self, prefix: str = "ctx_") -> dict:
        return {f"{prefix}{k}": v for k, v in asdict(self).items()}

    def relative_features(self, price: float, atr: float, prefix: str = "rel_") -> dict:
        """Signed distances from ``price`` to each level, in points and ATR."""
        out: dict[str, float | None] = {}
        for name, level in asdict(self).items():
            if level is None or name.endswith(("_size", "_range")):
                continue
            diff = price - level
            out[f"{prefix}{name}_points"] = diff
            out[f"{prefix}{name}_atr"] = diff / atr if atr > 0 else None
        return out

    def opening_range_position(self, price: float) -> float | None:
        """0 = at the opening-range low, 1 = at its high (can exceed [0,1])."""
        hi, lo = self.opening_range_long_high, self.opening_range_long_low
        if hi is None or lo is None or hi <= lo:
            return None
        return (price - lo) / (hi - lo)

    def overnight_range_position(self, price: float) -> float | None:
        hi, lo = self.overnight_high, self.overnight_low
        if hi is None or lo is None or hi <= lo:
            return None
        return (price - lo) / (hi - lo)


def _agg(df: pl.DataFrame) -> tuple[float | None, float | None]:
    if df.is_empty():
        return None, None
    return float(df["high"].max()), float(df["low"].min())


def _nearest_multiple(price: float, increment: float) -> float:
    return round(price / increment) * increment if increment > 0 else price


def compute_context_levels(
    session_bars: pl.DataFrame,
    prior_session_bars: pl.DataFrame | None,
    clock: SessionClock,
    config: ContextConfig,
    now: datetime,
    reference_price: float | None = None,
) -> ContextLevels:
    """Context for one session, using only bars up to and including ``now``."""
    ctx = ContextLevels()
    cash_open = clock.cash_open_dt(session_bars["globex_session_date"][0])
    upto = session_bars.filter(pl.col("timestamp_ny") <= now)

    if prior_session_bars is not None and not prior_session_bars.is_empty():
        prior_cash = prior_session_bars.filter(
            pl.col("session_segment") == "CASH"
            if "session_segment" in prior_session_bars.columns
            else pl.lit(True)
        )
        if not prior_cash.is_empty():
            ctx.prior_cash_high, ctx.prior_cash_low = _agg(prior_cash)
            ctx.prior_cash_close = float(prior_cash.sort("timestamp_utc")["close"][-1])

    globex = upto.filter(pl.col("timestamp_ny") < cash_open)
    ctx.globex_high, ctx.globex_low = _agg(globex)

    if "session_segment" in upto.columns:
        overnight = upto.filter(pl.col("session_segment") == "OVERNIGHT")
        premarket = upto.filter(pl.col("session_segment") == "PREMARKET")
        cash = upto.filter(pl.col("session_segment") == "CASH")
    else:
        premarket_start = clock.ny_datetime(
            session_bars["globex_session_date"][0], clock.config.premarket_start
        )
        overnight = upto.filter(pl.col("timestamp_ny") < premarket_start)
        premarket = upto.filter(
            (pl.col("timestamp_ny") >= premarket_start) & (pl.col("timestamp_ny") < cash_open)
        )
        cash = upto.filter(pl.col("timestamp_ny") >= cash_open)
    ctx.overnight_high, ctx.overnight_low = _agg(overnight)
    ctx.premarket_high, ctx.premarket_low = _agg(premarket)

    if not cash.is_empty():
        first = cash.sort("timestamp_utc").row(0, named=True)
        ctx.open_price = float(first["open"])
        ctx.cash_high_so_far, ctx.cash_low_so_far = _agg(cash)
        ctx.session_midpoint = (ctx.cash_high_so_far + ctx.cash_low_so_far) / 2
        ctx.developing_range = ctx.cash_high_so_far - ctx.cash_low_so_far

        for minutes, hi_attr, lo_attr, size_attr in (
            (config.opening_range_minutes_short, "opening_range_short_high",
             "opening_range_short_low", "opening_range_short_size"),
            (config.opening_range_minutes_long, "opening_range_long_high",
             "opening_range_long_low", "opening_range_long_size"),
        ):
            window = cash.filter(
                pl.col("timestamp_ny") < cash_open + pl.duration(minutes=minutes)
            )
            hi, lo = _agg(window)
            setattr(ctx, hi_attr, hi)
            setattr(ctx, lo_attr, lo)
            if hi is not None and lo is not None:
                setattr(ctx, size_attr, hi - lo)
        if ctx.opening_range_long_high is not None:
            ctx.opening_range_midpoint = (
                ctx.opening_range_long_high + ctx.opening_range_long_low
            ) / 2

    for minutes, hi_attr, lo_attr in (
        (15, "high_15m", "low_15m"),
        (30, "high_30m", "low_30m"),
        (60, "high_60m", "low_60m"),
    ):
        window = upto.filter(pl.col("timestamp_ny") > now - pl.duration(minutes=minutes))
        hi, lo = _agg(window)
        setattr(ctx, hi_attr, hi)
        setattr(ctx, lo_attr, lo)

    ref = reference_price
    if ref is None and not upto.is_empty():
        ref = float(upto.sort("timestamp_utc")["close"][-1])
    if ref is not None:
        ctx.nearest_whole_number = _nearest_multiple(ref, config.whole_number_increment)
        ctx.nearest_round_increment = _nearest_multiple(ref, config.round_number_increment)
    return ctx


def target_matches_context(price: float, ctx: ContextLevels, tolerance: float) -> dict:
    """Flag whether a target coincides with a named structural level."""
    def near(level: float | None) -> bool:
        return level is not None and abs(price - level) <= tolerance

    return {
        "target_is_15m_extreme": near(ctx.high_15m) or near(ctx.low_15m),
        "target_is_30m_extreme": near(ctx.high_30m) or near(ctx.low_30m),
        "target_is_60m_extreme": near(ctx.high_60m) or near(ctx.low_60m),
        "target_is_overnight_extreme": near(ctx.overnight_high) or near(ctx.overnight_low),
        "target_is_premarket_extreme": near(ctx.premarket_high) or near(ctx.premarket_low),
        "target_is_prior_cash_extreme": near(ctx.prior_cash_high) or near(ctx.prior_cash_low),
        "target_is_opening_range_extreme": (
            near(ctx.opening_range_long_high) or near(ctx.opening_range_long_low)
        ),
    }
