from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from fvg_backtest.analytics.stats import (
    compare_instruments,
    conditional_table,
    normalized_comparison,
    summarize_trades,
)
from fvg_backtest.analytics.walkforward import build_folds, consistency_report, grid_points
from fvg_backtest.config.schema import WalkForwardConfig


def _trades(results, **extra) -> pl.DataFrame:
    n = len(results)
    base = {
        "result_r": results,
        "net_result_r": [r - 0.06 for r in results],
        "mae_r": [0.3] * n,
        "mfe_r": [1.0] * n,
        "is_clean_win": [r > 0 for r in results],
        "is_sweaty_win": [False] * n,
        "is_stalled": [False] * n,
        "is_ranging": [False] * n,
        "duration_minutes": [10] * n,
        "minutes_to_target": [8 if r > 0 else None for r in results],
        "session_date": [date(2025, 1, 6) + timedelta(days=i) for i in range(n)],
    }
    base.update({k: v for k, v in extra.items()})
    return pl.DataFrame(base, strict=False)


def test_summary_block():
    s = summarize_trades(_trades([1.0, 1.0, -1.0, 2.0]), min_sample=2)
    assert s["trades"] == 4
    assert s["win_rate"] == 0.75
    assert s["expectancy_r"] == pytest.approx(0.75)
    assert s["net_expectancy_r"] == pytest.approx(0.69)
    assert s["avg_win_r"] == pytest.approx(4 / 3)
    assert s["avg_loss_r"] == pytest.approx(-1.0)
    assert s["profit_factor"] == pytest.approx(4.0)
    assert s["max_drawdown_r"] == pytest.approx(-1.0)
    assert s["reliable"] is True
    assert s["warning"] is None


def test_small_samples_are_flagged_not_ranked():
    s = summarize_trades(_trades([3.0, 3.0]), min_sample=20)
    assert s["expectancy_r"] == 3.0
    assert s["reliable"] is False
    assert "small sample" in s["warning"]


def test_empty_group():
    s = summarize_trades(pl.DataFrame({"result_r": [], "net_result_r": []}))
    assert s["trades"] == 0 and s["reliable"] is False


def test_conditional_table_groups_and_sorts_unreliable_last():
    trades = _trades(
        [1.0] * 3 + [-1.0] * 25,
        significance_type=["A_ONLY"] * 3 + ["A_AND_B"] * 25,
    )
    table = conditional_table(
        trades, "significance_type", min_sample=20, sort_by="expectancy_r"
    )
    assert table.height == 2
    # the 3-trade group has the better expectancy but must not top the table
    assert table["group"][0] == "A_AND_B"
    assert table["reliable"].to_list() == [True, False]
    assert table.filter(pl.col("group") == "A_ONLY")["warning"][0].startswith("small sample")


def test_conditional_table_binning():
    trades = _trades([1.0] * 10, target_distance_r=[0.5 + 0.4 * i for i in range(10)])
    table = conditional_table(trades, "target_distance_r", bins=2, min_sample=1)
    assert table.height == 2
    assert table["trades"].sum() == 10


def test_conditional_table_unknown_column():
    with pytest.raises(KeyError):
        conditional_table(_trades([1.0]), "nope")


def test_instruments_are_compared_never_pooled():
    nq = _trades([1.0, 1.0, -1.0], ambiguous_execution=[False] * 3)
    mnq = _trades([1.0, -1.0, -1.0], ambiguous_execution=[True, False, False])
    out = compare_instruments({"NQ": nq, "MNQ": mnq}, min_sample=2)
    assert out.height == 2
    assert set(out["group"]) == {"NQ", "MNQ"}
    assert out.filter(pl.col("group") == "NQ")["expectancy_r"][0] > 0
    assert out.filter(pl.col("group") == "MNQ")["ambiguous_rate"][0] == pytest.approx(1 / 3)


def test_normalized_comparison_uses_common_sessions_only():
    nq = _trades([1.0, 1.0, 1.0])                          # Jan 6, 7, 8
    mnq = _trades([-1.0, -1.0])                            # Jan 6, 7
    out = normalized_comparison({"NQ": nq, "MNQ": mnq}, min_sample=1)
    assert out["common_sessions"].unique().to_list() == [2]
    assert out.filter(pl.col("group") == "NQ")["trades"][0] == 2


# --------------------------------------------------------------------------
# walk-forward
# --------------------------------------------------------------------------


def test_folds_are_chronological_and_non_overlapping_within_a_fold():
    cfg = WalkForwardConfig(
        development_days=60, validation_days=20, out_of_sample_days=20, step_days=20
    )
    folds = build_folds(date(2025, 1, 1), date(2025, 12, 31), cfg)
    assert folds
    for f in folds:
        assert f.dev_end < f.val_start < f.val_end < f.oos_start <= f.oos_end
    # each fold starts later than the last: strictly forward in time
    starts = [f.dev_start for f in folds]
    assert starts == sorted(starts)
    assert starts[1] - starts[0] == timedelta(days=20)


def test_too_short_a_range_yields_no_folds():
    cfg = WalkForwardConfig(development_days=120, validation_days=30, out_of_sample_days=30)
    assert build_folds(date(2025, 1, 1), date(2025, 2, 1), cfg) == []


def test_grid_points_expands_the_product():
    pts = grid_points({"a": [1, 2], "b": [10, 20, 30]})
    assert len(pts) == 6
    assert {"a": 2, "b": 30} in pts
    assert grid_points({}) == [{}]


def test_walkforward_freezes_parameters_after_development():
    from fvg_backtest.analytics.walkforward import run_walkforward
    from fvg_backtest.config import AppConfig

    cfg = AppConfig(start="2025-01-01", end="2025-09-30")
    cfg.walkforward = WalkForwardConfig(
        development_days=60, validation_days=20, out_of_sample_days=20, step_days=60,
        min_trades_per_fold=2,
        grid={"significance.type_a.minimum_gap_atr": [0.05, 0.5]},
    )
    seen: list[tuple] = []

    def run_fn(c, a, b):
        threshold = c.significance.type_a.minimum_gap_atr
        seen.append((threshold, a, b))
        # 0.05 looks better in development, so it should be the frozen choice
        r = [1.0, 1.0, -1.0] if threshold == 0.05 else [1.0, -1.0, -1.0]
        return _trades(r)

    result = run_walkforward(cfg, run_fn)
    assert not result.folds.is_empty()
    assert result.parameters["significance.type_a.minimum_gap_atr"].to_list() == [0.05] * result.folds.height

    # per fold: the validation and out-of-sample windows were each run
    # exactly once, with the frozen value — never re-optimized
    for fold in result.folds.iter_rows(named=True):
        for phase_start, phase_end in (
            (fold["val_start"], fold["val_end"]), (fold["oos_start"], fold["oos_end"])
        ):
            runs = [s for s in seen if (s[1], s[2]) == (phase_start, phase_end)]
            assert len(runs) == 1
            assert runs[0][0] == 0.05
        # the development window, by contrast, tried every grid point
        dev_runs = [s for s in seen if (s[1], s[2]) == (fold["dev_start"], fold["dev_end"])]
        assert sorted(r[0] for r in dev_runs) == [0.05, 0.5]


def test_walkforward_warns_when_development_is_too_thin():
    from fvg_backtest.analytics.walkforward import run_walkforward
    from fvg_backtest.config import AppConfig

    cfg = AppConfig(start="2025-01-01", end="2025-09-30")
    cfg.walkforward = WalkForwardConfig(
        development_days=60, validation_days=20, out_of_sample_days=20, step_days=60,
        min_trades_per_fold=50, grid={},
    )
    result = run_walkforward(cfg, lambda c, a, b: _trades([1.0, -1.0]))
    assert result.folds.is_empty()
    assert any("fewer than" in w for w in result.warnings)


def test_consistency_report_flags_contract_dependence():
    trades = _trades(
        [1.0, 1.0, 1.0, -1.0, -1.0, -1.0],
        underlying_contract=["NQH25"] * 3 + ["NQM25"] * 3,
    )
    out = consistency_report(trades)
    assert out.height == 2
    assert out["inconsistent"].unique().to_list() == [True]
    assert out["groups_positive"][0] == 1
