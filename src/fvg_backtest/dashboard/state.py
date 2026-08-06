"""Dashboard state helpers, kept free of Streamlit imports.

Everything here is plain data access so it can be unit-tested without
spinning up a Streamlit session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

from ..config.loader import load_config
from ..config.schema import AppConfig

DASHBOARD_PAGES = [
    "Data",
    "Strategy settings",
    "Backtest",
    "Results",
    "Conditions explorer",
    "Trade explorer",
    "Range analysis",
    "Walk-forward",
    "Predictive models",
]

DEFAULT_CONFIG_PATH = Path("config/default.yaml")


def default_config() -> AppConfig:
    if DEFAULT_CONFIG_PATH.exists():
        return load_config(DEFAULT_CONFIG_PATH)
    return AppConfig()


@dataclass
class RunStore:
    """A run directory loaded from disk."""

    run_id: str
    path: Path
    summary: dict = field(default_factory=dict)
    config: AppConfig | None = None
    setups: pl.DataFrame = field(default_factory=pl.DataFrame)
    trades: pl.DataFrame = field(default_factory=pl.DataFrame)
    events: pl.DataFrame = field(default_factory=pl.DataFrame)
    contracts: pl.DataFrame = field(default_factory=pl.DataFrame)
    daily: pl.DataFrame = field(default_factory=pl.DataFrame)

    @property
    def instrument(self) -> str:
        return self.summary.get("instrument", "?")

    def sessions(self) -> list[date]:
        if self.setups.is_empty():
            return []
        return sorted(self.setups["session_date"].unique().to_list())

    def session_trades(self, session_date: date) -> list[dict]:
        if self.trades.is_empty():
            return []
        return self.trades.filter(pl.col("session_date") == session_date).to_dicts()

    def session_setup(self, session_date: date) -> dict | None:
        if self.setups.is_empty():
            return None
        rows = self.setups.filter(
            (pl.col("session_date") == session_date) & pl.col("selected")
        )
        return rows.to_dicts()[0] if not rows.is_empty() else None

    def session_events(self, session_date: date) -> pl.DataFrame:
        if self.events.is_empty():
            return pl.DataFrame()
        return self.events.filter(pl.col("session_date") == session_date)


def list_runs(runs_dir: str | Path = "runs") -> list[str]:
    base = Path(runs_dir)
    if not base.exists():
        return []
    runs = [
        p.name
        for p in base.iterdir()
        if p.is_dir() and (p / "summary.json").exists()
    ]
    return sorted(runs, reverse=True)


def load_run(run_id: str, runs_dir: str | Path = "runs") -> RunStore:
    base = Path(runs_dir) / run_id
    if not base.exists():
        raise FileNotFoundError(f"no run directory at {base}")
    store = RunStore(run_id=run_id, path=base)
    summary_path = base / "summary.json"
    if summary_path.exists():
        store.summary = json.loads(summary_path.read_text())
    config_path = base / "config.yaml"
    if config_path.exists():
        store.config = load_config(config_path)
    for attr, name in (
        ("setups", "setups"), ("trades", "trades"), ("events", "events"),
        ("contracts", "contracts"), ("daily", "daily_results"),
    ):
        p = base / f"{name}.parquet"
        if p.exists():
            setattr(store, attr, pl.read_parquet(p))
    return store


def session_frames(bars: pl.DataFrame, session_date: date) -> pl.DataFrame:
    if bars.is_empty():
        return bars
    return bars.filter(pl.col("globex_session_date") == session_date).sort("timestamp_utc")


def numeric_columns(df: pl.DataFrame) -> list[str]:
    return [
        c for c, dt in df.schema.items()
        if dt.is_numeric() and not c.endswith("_index")
    ]


def categorical_columns(df: pl.DataFrame) -> list[str]:
    return [
        c for c, dt in df.schema.items()
        if dt in (pl.String, pl.Boolean, pl.Categorical)
    ]
