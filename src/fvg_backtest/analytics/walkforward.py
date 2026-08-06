"""Chronological walk-forward validation.

Folds march forward through the calendar:

    |<-- development -->|<- validation ->|<- out-of-sample ->|
                        ^ thresholds frozen here

Thresholds are optimized **only** inside the development window; the winning
configuration is then frozen and applied unchanged to validation and
out-of-sample.  Random splitting is deliberately not offered as the primary
method — it leaks future information into the past.

Each fold reports in-sample / validation / out-of-sample statistics so a
filter that only works in one contract or one short period is visible rather
than flattering.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from ..config.schema import AppConfig, WalkForwardConfig
from .stats import summarize_trades


@dataclass
class Fold:
    index: int
    dev_start: date
    dev_end: date
    val_start: date
    val_end: date
    oos_start: date
    oos_end: date

    def to_dict(self) -> dict:
        return {
            "fold": self.index,
            "dev_start": self.dev_start, "dev_end": self.dev_end,
            "val_start": self.val_start, "val_end": self.val_end,
            "oos_start": self.oos_start, "oos_end": self.oos_end,
        }


@dataclass
class WalkForwardResult:
    folds: pl.DataFrame
    parameters: pl.DataFrame
    consistency: pl.DataFrame = field(default_factory=pl.DataFrame)
    warnings: list[str] = field(default_factory=list)


def build_folds(start: date, end: date, config: WalkForwardConfig) -> list[Fold]:
    folds: list[Fold] = []
    cursor = start
    i = 0
    while True:
        dev_end = cursor + timedelta(days=config.development_days - 1)
        val_start = dev_end + timedelta(days=1)
        val_end = val_start + timedelta(days=config.validation_days - 1)
        oos_start = val_end + timedelta(days=1)
        oos_end = oos_start + timedelta(days=config.out_of_sample_days - 1)
        if oos_start > end:
            break
        folds.append(
            Fold(i, cursor, dev_end, val_start, min(val_end, end), oos_start, min(oos_end, end))
        )
        cursor += timedelta(days=config.step_days)
        i += 1
        if cursor > end:
            break
    return folds


def _slice(trades: pl.DataFrame, a: date, b: date) -> pl.DataFrame:
    if trades.is_empty():
        return trades
    return trades.filter(
        (pl.col("session_date") >= a) & (pl.col("session_date") <= b)
    )


def grid_points(grid: dict[str, list[float]]) -> list[dict[str, float]]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def run_walkforward(
    config: AppConfig,
    run_fn,
    *,
    start: date | None = None,
    end: date | None = None,
) -> WalkForwardResult:
    """Walk forward over folds.

    ``run_fn(cfg, start, end) -> trades DataFrame`` performs one backtest;
    the caller supplies it so this module stays independent of data access.
    """
    wf = config.walkforward
    start = start or date.fromisoformat(config.start)
    end = end or date.fromisoformat(config.end)
    folds = build_folds(start, end, wf)
    if not folds:
        return WalkForwardResult(
            folds=pl.DataFrame(), parameters=pl.DataFrame(),
            warnings=["date range too short for one full fold"],
        )

    fold_rows: list[dict] = []
    param_rows: list[dict] = []
    warnings: list[str] = []
    points = grid_points(wf.grid)

    for fold in folds:
        best_params, best_score, best_trades = None, None, None
        for params in points:
            cfg = config.model_copy(deep=True)
            for path, value in params.items():
                cfg.set_by_path(path, value)
            dev_trades = run_fn(cfg, fold.dev_start, fold.dev_end)
            if dev_trades.height < wf.min_trades_per_fold:
                continue
            score = summarize_trades(dev_trades)[wf.optimize_metric]
            if score is None:
                continue
            if best_score is None or score > best_score:
                best_params, best_score, best_trades = params, score, dev_trades

        if best_params is None:
            warnings.append(
                f"fold {fold.index}: development window produced fewer than "
                f"{wf.min_trades_per_fold} trades for every parameter set"
            )
            continue

        frozen = config.model_copy(deep=True)
        for path, value in best_params.items():
            frozen.set_by_path(path, value)
        val_trades = run_fn(frozen, fold.val_start, fold.val_end)
        oos_trades = run_fn(frozen, fold.oos_start, fold.oos_end)

        row = fold.to_dict()
        row["parameters"] = str(best_params)
        for phase, df in (
            ("dev", best_trades), ("val", val_trades), ("oos", oos_trades)
        ):
            stats = summarize_trades(df, group=phase)
            row.update(
                {
                    f"{phase}_trades": stats["trades"],
                    f"{phase}_expectancy_r": stats["expectancy_r"],
                    f"{phase}_net_expectancy_r": stats["net_expectancy_r"],
                    f"{phase}_win_rate": stats["win_rate"],
                    f"{phase}_profit_factor": stats["profit_factor"],
                    f"{phase}_reliable": stats["reliable"],
                }
            )
        row["degradation_r"] = (
            (row["oos_expectancy_r"] or 0) - (row["dev_expectancy_r"] or 0)
        )
        row["holds_out_of_sample"] = bool(
            row["oos_trades"] and (row["oos_expectancy_r"] or 0) > 0
        )
        fold_rows.append(row)
        param_rows.append({"fold": fold.index, **best_params, "dev_score": best_score})

        if row["oos_trades"] and row["oos_trades"] < wf.min_trades_per_fold:
            warnings.append(
                f"fold {fold.index}: only {row['oos_trades']} out-of-sample trades — "
                "not enough to judge"
            )

    folds_df = pl.DataFrame(fold_rows, strict=False) if fold_rows else pl.DataFrame()
    params_df = pl.DataFrame(param_rows, strict=False) if param_rows else pl.DataFrame()
    if not folds_df.is_empty():
        held = folds_df["holds_out_of_sample"].sum()
        if held < len(fold_rows) / 2:
            warnings.append(
                f"only {held}/{len(fold_rows)} folds stayed positive out of sample — "
                "the optimized thresholds are likely fitted to their development window"
            )
    return WalkForwardResult(folds=folds_df, parameters=params_df, warnings=warnings)


def consistency_report(trades: pl.DataFrame, by: str = "underlying_contract") -> pl.DataFrame:
    """Per-contract (or per-instrument) stability of the same settings."""
    if trades.is_empty() or by not in trades.columns:
        return pl.DataFrame()
    rows = []
    for (key,), part in trades.group_by([by], maintain_order=True):
        rows.append({by: key, **summarize_trades(part, group=str(key))})
    out = pl.DataFrame(rows, strict=False)
    positive = out.filter(pl.col("expectancy_r") > 0).height
    return out.with_columns(
        pl.lit(positive).alias("groups_positive"),
        pl.lit(out.height).alias("groups_total"),
        (pl.lit(positive) < pl.lit(out.height)).alias("inconsistent"),
    )
