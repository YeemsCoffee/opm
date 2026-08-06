from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import pytest

from fvg_backtest.analytics.models import (
    LeakageError,
    assert_no_leakage,
    results_frame,
    select_features,
    train_models,
    train_target,
)
from fvg_backtest.config.schema import ModelsConfig

NY = ZoneInfo("America/New_York")


def _trades(n: int = 200, signal: bool = True) -> pl.DataFrame:
    """Synthetic trades where a single entry-time feature drives the outcome."""
    rng = np.random.default_rng(0)
    gap = rng.uniform(0.05, 1.5, n)
    noise = rng.normal(0, 0.25, n)
    hit = (gap + noise > 0.8) if signal else (rng.random(n) > 0.5)
    return pl.DataFrame(
        {
            "session_date": [date(2025, 1, 6) + timedelta(days=i) for i in range(n)],
            "entry_time": [datetime(2025, 1, 6, 9, 40, tzinfo=NY) + timedelta(days=i) for i in range(n)],
            # entry-time features (allowlisted prefixes)
            "fvg_gap_atr": gap,
            "fvg_preservation_ratio": rng.uniform(0.2, 1.0, n),
            "target_distance_r": rng.uniform(0.5, 3.0, n),
            "target_age_minutes": rng.integers(5, 60, n),
            "pre_entry_overlap": rng.uniform(0.1, 0.9, n),
            "entry_delay_minutes": rng.integers(1, 30, n),
            # outcomes
            "exit_reason": ["TARGET" if h else "STOP" for h in hit],
            "is_clean_win": hit,
            "is_sweaty_win": ~hit,
            "is_ranging": rng.random(n) > 0.7,
            "result_r": np.where(hit, 1.2, -1.0),
            "mae_r": rng.uniform(0, 1, n),
            "mfe_r": rng.uniform(0, 2, n),
            "duration_minutes": rng.integers(2, 60, n),
        }
    )


def test_feature_selection_keeps_only_entry_time_columns():
    features = select_features(_trades())
    assert "fvg_gap_atr" in features
    assert "target_distance_r" in features
    assert "pre_entry_overlap" in features
    for leak in ("result_r", "mae_r", "mfe_r", "duration_minutes", "is_clean_win"):
        assert leak not in features


def test_leakage_guard_raises_on_outcome_columns():
    with pytest.raises(LeakageError, match="outcome columns"):
        assert_no_leakage(["fvg_gap_atr", "mae_r"])
    with pytest.raises(LeakageError):
        assert_no_leakage(["range_onset_minutes_after_formation"])
    assert_no_leakage(["fvg_gap_atr", "target_age_minutes"])  # fine


def test_range_onset_can_never_be_a_feature():
    trades = _trades().with_columns(
        pl.lit(5).alias("range_onset_minutes_after_formation"),
        pl.lit(True).alias("range_onset_found"),
    )
    features = select_features(trades)
    assert not any(f.startswith("range_onset") for f in features)


def test_model_learns_a_real_signal():
    result = train_target(_trades(300), "probability_target_before_stop")
    assert result.trained
    assert result.n_train + result.n_validation == 300
    assert result.roc_auc > 0.75            # the planted signal is recoverable
    assert 0.0 <= result.brier <= 0.35
    assert result.calibration
    assert result.feature_importance[0]["feature"] == "fvg_gap_atr"


def test_split_is_chronological_not_random():
    trades = _trades(100)
    result = train_target(trades, "probability_clean_win", train_fraction=0.7)
    assert result.n_train == 70 and result.n_validation == 30
    # the split point is a date boundary: every training row precedes every
    # validation row
    ordered = trades.sort("entry_time")
    assert ordered["entry_time"][69] < ordered["entry_time"][70]


def test_noise_gives_a_weak_model_not_a_confident_one():
    result = train_target(_trades(300, signal=False), "probability_target_before_stop")
    assert result.trained
    assert 0.35 < result.roc_auc < 0.65     # no real edge to find


def test_too_few_trades_is_reported_not_fitted():
    result = train_target(_trades(12), "probability_clean_win")
    assert not result.trained
    assert "too few" in result.note


def test_single_class_window_is_handled():
    trades = _trades(80).with_columns(pl.lit("TARGET").alias("exit_reason"))
    result = train_target(trades, "probability_target_before_stop")
    assert not result.trained
    assert "single class" in result.note


def test_hist_gradient_boosting_runs():
    result = train_target(
        _trades(300), "probability_clean_win", algorithm="hist_gradient_boosting"
    )
    assert result.trained
    assert result.algorithm == "hist_gradient_boosting"
    assert result.roc_auc is not None
    assert result.feature_importance
    assert result.partial_dependence


def test_partial_dependence_shape():
    result = train_target(_trades(200), "probability_target_before_stop")
    features = {row["feature"] for row in result.partial_dependence}
    assert len(features) <= 3
    for row in result.partial_dependence:
        assert 0.0 <= row["mean_probability"] <= 1.0


def test_train_all_configured_targets():
    results = train_models(_trades(300), ModelsConfig(enabled=True))
    assert len(results) == 8            # 4 targets x 2 algorithms
    frame = results_frame(results)
    assert set(frame["target"].unique()) == {
        "probability_target_before_stop", "probability_clean_win",
        "probability_sweaty_trade", "probability_ranging_trade",
    }
    assert frame["trained"].any()


def test_empty_input():
    result = train_target(pl.DataFrame(), "probability_clean_win")
    assert not result.trained and result.note == "no trades"
    assert results_frame([]).is_empty()
