"""Conditional performance tables.

Every group reports the same block of statistics, plus an explicit
small-sample warning.  Groups below ``min_sample`` are never presented as
reliable: they carry ``reliable=False`` and a warning string, and the
sorting helpers push them down rather than letting a 2-trade group top the
table.

Confidence intervals on expectancy use the normal approximation of the
standard error of the mean (``1.96 * s / sqrt(n)``), which is what a
research dashboard needs — a coarse ruler for "is this distinguishable from
zero", not an inferential guarantee.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import polars as pl

MIN_SAMPLE = 20

STAT_COLUMNS = [
    "group",
    "trades",
    "win_rate",
    "expectancy_r",
    "net_expectancy_r",
    "avg_win_r",
    "avg_loss_r",
    "profit_factor",
    "median_r",
    "median_mae_r",
    "median_mfe_r",
    "clean_win_rate",
    "sweaty_win_rate",
    "stalled_rate",
    "ranging_rate",
    "median_minutes_to_target",
    "median_duration",
    "max_drawdown_r",
    "expectancy_ci_low",
    "expectancy_ci_high",
    "reliable",
    "warning",
]


# the columns worth showing in a terminal; the full block stays in the frame
KEY_STAT_COLUMNS = [
    "group",
    "trades",
    "win_rate",
    "expectancy_r",
    "net_expectancy_r",
    "profit_factor",
    "median_mae_r",
    "median_mfe_r",
    "clean_win_rate",
    "sweaty_win_rate",
    "ranging_rate",
    "reliable",
]


def key_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Narrow a stats table to the headline columns (for terminal output)."""
    present = [c for c in KEY_STAT_COLUMNS if c in df.columns]
    return df.select(present) if present else df


@dataclass
class GroupStats:
    group: str
    trades: int
    stats: dict

    @property
    def reliable(self) -> bool:
        return bool(self.stats.get("reliable"))


def _max_drawdown(rs: np.ndarray) -> float:
    if len(rs) == 0:
        return 0.0
    equity = np.cumsum(rs)
    peak = np.maximum.accumulate(equity)
    return float(np.min(equity - peak))


def _safe_median(series: pl.Series) -> float | None:
    clean = series.drop_nulls()
    return float(clean.median()) if len(clean) else None


def summarize_trades(
    trades: pl.DataFrame, group: str = "ALL", min_sample: int = MIN_SAMPLE
) -> dict:
    """The standard statistics block for one set of trades."""
    n = trades.height
    if n == 0:
        return {
            "group": group, "trades": 0, "reliable": False,
            "warning": "no trades", **{c: None for c in STAT_COLUMNS[2:-2]},
        }
    r = trades["result_r"].to_numpy().astype(float)
    net_r = trades["net_result_r"].to_numpy().astype(float)
    wins = r[r > 0]
    losses = r[r <= 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    sd = float(np.std(r, ddof=1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 0 else 0.0
    mean_r = float(np.mean(r))

    def rate(col: str) -> float | None:
        if col not in trades.columns:
            return None
        vals = trades[col].fill_null(False).cast(pl.Boolean)
        return float(vals.mean())

    warnings = []
    if n < min_sample:
        warnings.append(f"small sample (n={n} < {min_sample})")
    if n and len(wins) == 0:
        warnings.append("no winning trades")

    return {
        "group": group,
        "trades": n,
        "win_rate": float(np.mean(r > 0)),
        "expectancy_r": mean_r,
        "net_expectancy_r": float(np.mean(net_r)),
        "avg_win_r": float(np.mean(wins)) if len(wins) else None,
        "avg_loss_r": float(np.mean(losses)) if len(losses) else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "median_r": float(np.median(r)),
        "median_mae_r": _safe_median(trades["mae_r"]) if "mae_r" in trades.columns else None,
        "median_mfe_r": _safe_median(trades["mfe_r"]) if "mfe_r" in trades.columns else None,
        "clean_win_rate": rate("is_clean_win"),
        "sweaty_win_rate": rate("is_sweaty_win"),
        "stalled_rate": rate("is_stalled"),
        "ranging_rate": rate("is_ranging"),
        "median_minutes_to_target": (
            _safe_median(trades["minutes_to_target"])
            if "minutes_to_target" in trades.columns
            else None
        ),
        "median_duration": (
            _safe_median(trades["duration_minutes"])
            if "duration_minutes" in trades.columns
            else None
        ),
        "max_drawdown_r": _max_drawdown(r),
        "expectancy_ci_low": mean_r - 1.96 * se,
        "expectancy_ci_high": mean_r + 1.96 * se,
        "reliable": n >= min_sample,
        "warning": "; ".join(warnings) or None,
    }


def conditional_table(
    trades: pl.DataFrame,
    by: str | list[str],
    *,
    bins: int | list[float] | None = None,
    labels: list[str] | None = None,
    min_sample: int = MIN_SAMPLE,
    sort_by: str | None = None,
) -> pl.DataFrame:
    """Statistics per group of ``by``; numeric columns can be binned first."""
    if trades.is_empty():
        return pl.DataFrame(schema={c: pl.String for c in STAT_COLUMNS})
    keys = [by] if isinstance(by, str) else list(by)
    missing = [k for k in keys if k not in trades.columns]
    if missing:
        raise KeyError(f"unknown column(s) {missing}")

    work = trades
    if bins is not None and len(keys) == 1:
        col = keys[0]
        work = _bin_column(work, col, bins, labels)
        keys = [f"{col}_bin"]

    rows = []
    for key_vals, part in work.group_by(keys, maintain_order=True):
        name = " | ".join(str(v) for v in key_vals)
        rows.append(summarize_trades(part, group=name, min_sample=min_sample))
    out = pl.DataFrame(rows, strict=False)
    if sort_by and sort_by in out.columns:
        # unreliable groups always sort last, never topping the table
        out = out.sort(["reliable", sort_by], descending=[True, True])
    else:
        out = out.sort("group")
    return out


def _bin_column(
    df: pl.DataFrame, col: str, bins: int | list[float], labels: list[str] | None
) -> pl.DataFrame:
    values = df[col].drop_nulls()
    if values.is_empty():
        return df.with_columns(pl.lit("no data").alias(f"{col}_bin"))
    if isinstance(bins, int):
        qs = [values.quantile(i / bins) for i in range(1, bins)]
        edges = sorted({q for q in qs if q is not None})
    else:
        edges = sorted(bins)
    if not edges:
        return df.with_columns(pl.lit("all").alias(f"{col}_bin"))
    return df.with_columns(
        pl.col(col)
        .cut(edges, labels=labels, left_closed=True)
        .cast(pl.String)
        .alias(f"{col}_bin")
    )


def compare_instruments(
    trades_by_instrument: dict[str, pl.DataFrame], min_sample: int = MIN_SAMPLE
) -> pl.DataFrame:
    """NQ vs MNQ side by side — reported separately, never pooled."""
    rows = [
        {
            **summarize_trades(df, group=name, min_sample=min_sample),
            "signal_count": df.height,
            "ambiguous_rate": (
                float(df["ambiguous_execution"].fill_null(False).cast(pl.Boolean).mean())
                if "ambiguous_execution" in df.columns and df.height
                else None
            ),
        }
        for name, df in trades_by_instrument.items()
    ]
    return pl.DataFrame(rows, strict=False)


def normalized_comparison(
    trades_by_instrument: dict[str, pl.DataFrame], min_sample: int = MIN_SAMPLE
) -> pl.DataFrame:
    """Same dates only, so instrument differences aren't calendar differences."""
    frames = {k: v for k, v in trades_by_instrument.items() if not v.is_empty()}
    if len(frames) < 2:
        return compare_instruments(trades_by_instrument, min_sample)
    common: set | None = None
    for df in frames.values():
        dates = set(df["session_date"].to_list())
        common = dates if common is None else (common & dates)
    trimmed = {
        k: v.filter(pl.col("session_date").is_in(list(common or [])))
        for k, v in frames.items()
    }
    out = compare_instruments(trimmed, min_sample)
    return out.with_columns(pl.lit(len(common or [])).alias("common_sessions"))
