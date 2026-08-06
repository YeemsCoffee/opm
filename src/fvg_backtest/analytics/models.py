"""Optional predictive models.

Strictly secondary to the descriptive system.  Four targets:

    probability_target_before_stop
    probability_clean_win
    probability_sweaty_trade
    probability_ranging_trade

Leakage protection is structural, not advisory:

- features come from the entry-time allowlist
  (:mod:`fvg_backtest.features.builder`), so outcome columns, exit prices,
  MAE/MFE and retrospective range-onset labels can never enter the matrix;
- the split is chronological — the training set is strictly earlier than the
  validation set, never a random shuffle;
- :func:`assert_no_leakage` re-checks the final column list and raises if a
  known-outcome column slipped in.

Outputs are probabilities with calibration and error metrics attached.  The
dashboard presents them as probabilities, never as certainties.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from ..config.schema import ModelsConfig
from ..features.builder import is_entry_time_feature

# columns that describe what happened — never features
OUTCOME_PREFIXES = (
    "result_", "net_result_", "gross_", "net_", "exit_", "mae_", "mfe_",
    "minutes_to_", "duration_", "is_clean", "is_sweaty", "is_stalled",
    "is_ranging", "is_immediate", "trade_label", "win", "range_onset_",
    "clean_", "sweaty_", "range_", "ambiguous_", "bars_in_trade",
    "entry_crossings", "midpoint_crossings", "closes_inside_zone",
    "direction_changes", "returned_to_", "approached_", "post_entry_overlap",
    "efficiency_ratio_10", "inversion_while_open", "mitigations_after_entry",
    "zone_state_final", "zone_touch_count", "zone_max_penetration",
    "zone_inversion_count", "zone_midpoint_crossings", "zone_complete_crossings",
    "zone_closes_inside", "zone_minutes_inside", "zone_wicks_through",
    "zone_distal_touches", "zone_midpoint_touches", "zone_first_touch",
    "zone_last_inversion", "max_penetration", "had_data_gap", "overlapped_",
    "crossed_open_price",
)

TARGET_BUILDERS = {
    "probability_target_before_stop": lambda df: (pl.col("exit_reason") == "TARGET"),
    "probability_clean_win": lambda df: pl.col("is_clean_win").fill_null(False),
    "probability_sweaty_trade": lambda df: pl.col("is_sweaty_win").fill_null(False),
    "probability_ranging_trade": lambda df: pl.col("is_ranging").fill_null(False),
}


class LeakageError(RuntimeError):
    pass


def assert_no_leakage(columns: list[str]) -> None:
    """Raise if any feature column describes the outcome."""
    bad = [c for c in columns if c.startswith(OUTCOME_PREFIXES)]
    if bad:
        raise LeakageError(
            "outcome columns cannot be used as features: " + ", ".join(sorted(bad))
        )


def select_features(trades: pl.DataFrame) -> list[str]:
    """Entry-time numeric/boolean columns, with outcomes excluded."""
    out = []
    for name, dtype in trades.schema.items():
        if not is_entry_time_feature(name):
            continue
        if name.startswith(OUTCOME_PREFIXES):
            continue
        if dtype.is_numeric() or dtype == pl.Boolean:
            out.append(name)
    assert_no_leakage(out)
    return sorted(out)


def _matrix(trades: pl.DataFrame, features: list[str]) -> np.ndarray:
    frame = trades.select(
        [pl.col(c).cast(pl.Float64, strict=False).alias(c) for c in features]
    )
    arr = frame.to_numpy().astype(float)
    # median imputation keeps rows with an occasional missing feature
    for j in range(arr.shape[1]):
        col = arr[:, j]
        if np.all(np.isnan(col)):
            arr[:, j] = 0.0
            continue
        arr[np.isnan(col), j] = float(np.nanmedian(col))
    return arr


@dataclass
class ModelResult:
    target: str
    algorithm: str
    trained: bool
    n_train: int
    n_validation: int
    positive_rate_train: float | None = None
    positive_rate_validation: float | None = None
    roc_auc: float | None = None
    precision: float | None = None
    recall: float | None = None
    brier: float | None = None
    calibration: list[dict] = field(default_factory=list)
    feature_importance: list[dict] = field(default_factory=list)
    partial_dependence: list[dict] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "target": self.target, "algorithm": self.algorithm, "trained": self.trained,
            "n_train": self.n_train, "n_validation": self.n_validation,
            "roc_auc": self.roc_auc, "precision": self.precision, "recall": self.recall,
            "brier": self.brier, "note": self.note,
            "positive_rate_train": self.positive_rate_train,
            "positive_rate_validation": self.positive_rate_validation,
        }


def _calibration_table(y: np.ndarray, p: np.ndarray, bins: int = 5) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if not mask.any():
            continue
        rows.append(
            {
                "bucket": f"{lo:.1f}–{hi:.1f}",
                "n": int(mask.sum()),
                "mean_predicted": float(p[mask].mean()),
                "observed_rate": float(y[mask].mean()),
            }
        )
    return rows


def train_target(
    trades: pl.DataFrame,
    target: str,
    *,
    algorithm: str = "logistic",
    features: list[str] | None = None,
    train_fraction: float = 0.7,
    min_rows: int = 40,
) -> ModelResult:
    """Fit one target with a chronological split."""
    if target not in TARGET_BUILDERS:
        raise ValueError(f"unknown target {target!r}")
    if trades.is_empty():
        return ModelResult(target, algorithm, False, 0, 0, note="no trades")

    ordered = trades.sort("entry_time" if "entry_time" in trades.columns else "session_date")
    features = features or select_features(ordered)
    assert_no_leakage(features)
    if not features:
        return ModelResult(target, algorithm, False, 0, 0, note="no usable features")

    y = ordered.select(TARGET_BUILDERS[target](ordered).alias("y"))["y"].cast(pl.Int8).to_numpy()
    X = _matrix(ordered, features)
    n = len(y)
    if n < min_rows:
        return ModelResult(
            target, algorithm, False, n, 0,
            note=f"only {n} trades — too few to model (need {min_rows})",
        )

    split = int(n * train_fraction)
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    if len(np.unique(y_train)) < 2 or len(y_val) == 0:
        return ModelResult(
            target, algorithm, False, len(y_train), len(y_val),
            note="training window contains a single class",
        )

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (
        brier_score_loss, precision_score, recall_score, roc_auc_score,
    )
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if algorithm == "logistic":
        model = make_pipeline(
            StandardScaler(), LogisticRegression(max_iter=2000, class_weight="balanced")
        )
    elif algorithm == "hist_gradient_boosting":
        model = HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.06, max_depth=3, random_state=0
        )
    else:
        raise ValueError(f"unknown algorithm {algorithm!r}")

    model.fit(X_train, y_train)
    proba = model.predict_proba(X_val)[:, 1]
    pred = (proba >= 0.5).astype(int)

    result = ModelResult(
        target=target,
        algorithm=algorithm,
        trained=True,
        n_train=len(y_train),
        n_validation=len(y_val),
        positive_rate_train=float(y_train.mean()),
        positive_rate_validation=float(y_val.mean()),
        precision=float(precision_score(y_val, pred, zero_division=0)),
        recall=float(recall_score(y_val, pred, zero_division=0)),
        brier=float(brier_score_loss(y_val, proba)),
        calibration=_calibration_table(y_val, proba),
    )
    if len(np.unique(y_val)) > 1:
        result.roc_auc = float(roc_auc_score(y_val, proba))
    else:
        result.note = "validation window contains a single class — ROC-AUC undefined"

    result.feature_importance = _importance(model, algorithm, features, X_val, y_val)
    top = [f["feature"] for f in result.feature_importance[:3]]
    result.partial_dependence = _partial_dependence(model, X_val, features, top)
    return result


def _importance(model, algorithm, features, X_val, y_val) -> list[dict]:
    if algorithm == "logistic":
        coefs = model[-1].coef_[0]
        rows = [
            {"feature": f, "importance": float(abs(c)), "coefficient": float(c)}
            for f, c in zip(features, coefs)
        ]
    else:
        from sklearn.inspection import permutation_importance

        try:
            imp = permutation_importance(
                model, X_val, y_val, n_repeats=5, random_state=0, scoring="roc_auc"
            )
            rows = [
                {"feature": f, "importance": float(m)}
                for f, m in zip(features, imp.importances_mean)
            ]
        except Exception:  # pragma: no cover - degenerate validation windows
            rows = [{"feature": f, "importance": 0.0} for f in features]
    return sorted(rows, key=lambda r: r["importance"], reverse=True)


def _partial_dependence(model, X, features, chosen, grid: int = 8) -> list[dict]:
    """Marginal effect of each chosen feature, averaged over the sample."""
    rows = []
    for name in chosen:
        j = features.index(name)
        col = X[:, j]
        lo, hi = np.percentile(col, [5, 95])
        if not np.isfinite([lo, hi]).all() or hi <= lo:
            continue
        for value in np.linspace(lo, hi, grid):
            probe = X.copy()
            probe[:, j] = value
            rows.append(
                {
                    "feature": name,
                    "value": float(value),
                    "mean_probability": float(model.predict_proba(probe)[:, 1].mean()),
                }
            )
    return rows


def train_models(trades: pl.DataFrame, config: ModelsConfig) -> list[ModelResult]:
    """Fit every configured target x algorithm pair."""
    out: list[ModelResult] = []
    features = select_features(trades) if not trades.is_empty() else []
    for target in config.targets:
        for algorithm in config.algorithms:
            out.append(
                train_target(
                    trades, target, algorithm=algorithm, features=features,
                    train_fraction=config.train_fraction,
                )
            )
    return out


def results_frame(results: list[ModelResult]) -> pl.DataFrame:
    return pl.DataFrame([r.to_dict() for r in results], strict=False) if results else pl.DataFrame()
