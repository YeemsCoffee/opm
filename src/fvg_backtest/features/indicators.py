"""Bar-level indicators computed without lookahead.

Every value at index ``i`` uses only bars ``<= i``.  ATR is the Wilder
average of true range over ``length`` bars (SMA optional), computed on the
one-minute series including Globex/premarket bars when the session config
allows it, so the 9:30 open already has a warmed-up ATR.
"""

from __future__ import annotations

import numpy as np
import polars as pl


def true_range(df: pl.DataFrame) -> pl.Series:
    prev_close = df["close"].shift(1)
    tr = np.maximum(
        (df["high"] - df["low"]).to_numpy(),
        np.maximum(
            np.abs((df["high"] - prev_close).to_numpy()),
            np.abs((df["low"] - prev_close).to_numpy()),
        ),
    )
    tr[0] = df["high"][0] - df["low"][0]
    return pl.Series("true_range", tr)


def wilder_atr(tr: np.ndarray, length: int) -> np.ndarray:
    """Wilder smoothing; warm-up uses the expanding mean so early bars have a
    usable (if noisier) value instead of nulls."""
    out = np.empty_like(tr, dtype=float)
    out[:] = np.nan
    if len(tr) == 0:
        return out
    running = 0.0
    for i, v in enumerate(tr):
        if i < length:
            running += v
            out[i] = running / (i + 1)
        else:
            out[i] = (out[i - 1] * (length - 1) + v) / length
    return out


def add_indicators(df: pl.DataFrame, atr_length: int = 20, method: str = "WILDER") -> pl.DataFrame:
    """Attach true_range, atr, body/wick geometry and rolling volume norms."""
    tr = true_range(df)
    tr_np = tr.to_numpy().astype(float)
    if method == "WILDER":
        atr = wilder_atr(tr_np, atr_length)
    else:
        atr = (
            pl.Series(tr_np)
            .rolling_mean(window_size=atr_length, min_samples=1)
            .to_numpy()
        )
    body_top = np.maximum(df["open"].to_numpy(), df["close"].to_numpy())
    body_bottom = np.minimum(df["open"].to_numpy(), df["close"].to_numpy())
    rng = (df["high"] - df["low"]).to_numpy()
    upper = df["high"].to_numpy() - body_top
    lower = body_bottom - df["low"].to_numpy()

    return df.with_columns(
        tr.alias("true_range"),
        pl.Series("atr", atr),
        pl.Series("body_top", body_top),
        pl.Series("body_bottom", body_bottom),
        pl.Series("body_size", np.abs(df["close"].to_numpy() - df["open"].to_numpy())),
        pl.Series("candle_range", rng),
        pl.Series("upper_wick", upper),
        pl.Series("lower_wick", lower),
        pl.Series("upper_wick_share", np.where(rng > 0, upper / np.where(rng > 0, rng, 1), 0.0)),
        pl.Series("lower_wick_share", np.where(rng > 0, lower / np.where(rng > 0, rng, 1), 0.0)),
    )


def efficiency_ratio(closes: np.ndarray) -> float:
    """|net move| / sum(|bar-to-bar moves|) over the supplied closes."""
    if len(closes) < 2:
        return 0.0
    net = abs(closes[-1] - closes[0])
    path = np.abs(np.diff(closes)).sum()
    return float(net / path) if path > 0 else 0.0


def candle_overlap(highs: np.ndarray, lows: np.ndarray) -> float:
    """Mean fractional overlap of consecutive candle ranges (0..1).

    High overlap = price revisiting the same band = range-like behavior.
    """
    if len(highs) < 2:
        return 0.0
    overlaps = []
    for i in range(1, len(highs)):
        lo = max(lows[i], lows[i - 1])
        hi = min(highs[i], highs[i - 1])
        inter = max(0.0, hi - lo)
        union = max(highs[i], highs[i - 1]) - min(lows[i], lows[i - 1])
        overlaps.append(inter / union if union > 0 else 1.0)
    return float(np.mean(overlaps))
