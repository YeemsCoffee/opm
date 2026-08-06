"""End-to-end run orchestration.

Loads data, builds the research series, runs every session, computes
features and metrics, and persists a fully reproducible run directory::

    runs/{run_id}/config.yaml        the exact resolved configuration
    runs/{run_id}/contracts.parquet  which contract each session came from
    runs/{run_id}/setups.parquet     every FVG candidate, including rejects
    runs/{run_id}/trades.parquet     one row per filled trade
    runs/{run_id}/events.parquet     the full event log
    runs/{run_id}/daily_results.parquet
    runs/{run_id}/summary.json
    runs/{run_id}/report.html
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from .analytics.trade_metrics import compute_trade_metrics, range_onset
from .config.loader import save_config
from .config.schema import AppConfig
from .data.file_provider import FileProvider, ParquetProvider
from .data.provider import MarketDataProvider
from .data.quality import DataQualityReport, validate_candles
from .execution.costs import CostModel
from .execution.simulator import SessionResult, TradeSimulator
from .features.builder import build_setup_features
from .features.indicators import add_indicators
from .futures.rolls import classify_roll_period
from .futures.series import build_continuous_series
from .sessions.calendar import TradingCalendar
from .sessions.clock import SessionClock

UTC = timezone.utc


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def make_provider(config: AppConfig, clock: SessionClock) -> MarketDataProvider:
    kind = config.data.provider
    if kind == "csv":
        if not config.data.path:
            raise ValueError("data.provider=csv requires data.path")
        return FileProvider(config.data.path, clock, config.instruments, source_label="csv")
    if kind == "parquet":
        if not config.data.path:
            raise ValueError("data.provider=parquet requires data.path")
        return ParquetProvider(config.data.path, clock, config.instruments, source_label="parquet")
    if kind == "databento":
        from .data.databento_provider import DatabentoProvider

        return DatabentoProvider(clock, config.instruments, config.data)
    if kind == "synthetic":
        from .data.synthetic import SyntheticProvider

        return SyntheticProvider(clock, config.instruments)
    raise ValueError(f"unknown provider {kind!r}")


@dataclass
class RunOutput:
    run_id: str
    config: AppConfig
    setups: pl.DataFrame
    trades: pl.DataFrame
    events: pl.DataFrame
    contracts: pl.DataFrame
    daily: pl.DataFrame
    summary: dict
    quality: DataQualityReport | None = None
    session_results: list[SessionResult] = field(default_factory=list)
    bars: pl.DataFrame | None = None

    def write(self, root: str | Path | None = None) -> Path:
        base = Path(root or self.config.runs_dir) / self.run_id
        base.mkdir(parents=True, exist_ok=True)
        save_config(self.config, base / "config.yaml")
        for name, df in (
            ("contracts", self.contracts),
            ("setups", self.setups),
            ("trades", self.trades),
            ("events", self.events),
            ("daily_results", self.daily),
        ):
            df.write_parquet(base / f"{name}.parquet")
        (base / "summary.json").write_text(json.dumps(self.summary, indent=2, default=str))
        (base / "report.html").write_text(render_report(self))
        return base


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------


def load_bars(
    config: AppConfig,
    clock: SessionClock,
    provider: MarketDataProvider | None = None,
    *,
    timeframe: str = "1m",
) -> tuple[pl.DataFrame, DataQualityReport | None]:
    config.validate_runnable()
    provider = provider or make_provider(config, clock)
    start_d = date.fromisoformat(config.start)
    end_d = date.fromisoformat(config.end)

    # reach back one session so the first day has Globex context
    start = clock.ny_datetime(start_d - timedelta(days=4), "18:00").astimezone(UTC)
    end = clock.ny_datetime(end_d + timedelta(days=1), "17:00").astimezone(UTC)

    symbol = config.contract if config.contract_mode == "DATED" else config.instrument
    bars = provider.get_bars(symbol, start, end, timeframe, config.contract_mode)

    instrument = config.active_instrument
    if config.contract_mode == "CONTINUOUS":
        bars, _ = build_continuous_series(bars, instrument, config.rolls)
    else:
        contract = config.contract or ""
        periods = {}
        for d in bars["globex_session_date"].unique().to_list():
            p, dte = classify_roll_period(d, contract, instrument, config.rolls)
            periods[d] = (str(p), dte)
        bars = bars.with_columns(
            pl.lit("DATED").alias("roll_method"),
            pl.col("globex_session_date")
            .map_elements(lambda d: periods[d][1], return_dtype=pl.Int64)
            .alias("days_to_expiration"),
            pl.col("globex_session_date")
            .map_elements(lambda d: periods[d][0], return_dtype=pl.String)
            .alias("roll_period"),
        )
        before = bars.height
        if config.rolls.exclude_rollover_sessions:
            bars = bars.filter(pl.col("roll_period") != "ROLLOVER_TRANSITION")
        if config.rolls.exclude_expiration_week:
            bars = bars.filter(pl.col("roll_period") != "EXPIRATION_WEEK")
        if before and bars.is_empty():
            raise ValueError(
                "the roll-period exclusions removed every session in this range — "
                "widen the dates or turn off the exclusions"
            )

    quality = validate_candles(bars, clock) if config.data.validate_quality else None
    if quality and not quality.ok and config.data.fail_on_quality_errors:
        raise ValueError(
            "data quality errors: " + "; ".join(i.message for i in quality.errors)
        )
    bars = bars.with_columns(
        pl.col("timestamp_ny")
        .map_elements(lambda t: str(clock.segment(t)), return_dtype=pl.String)
        .alias("session_segment")
    )
    return bars, quality


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def run_backtest(
    config: AppConfig,
    *,
    provider: MarketDataProvider | None = None,
    bars: pl.DataFrame | None = None,
    run_id: str | None = None,
    calendar: TradingCalendar | None = None,
    keep_bars: bool = False,
    finer_bars: pl.DataFrame | None = None,
) -> RunOutput:
    config.validate_runnable()
    calendar = calendar or TradingCalendar()
    clock = SessionClock(config=config.sessions, calendar=calendar)
    quality = None
    if bars is None:
        bars, quality = load_bars(config, clock, provider)

    start_d = date.fromisoformat(config.start) if config.start else bars["globex_session_date"].min()
    end_d = date.fromisoformat(config.end) if config.end else bars["globex_session_date"].max()

    costs = CostModel(config.active_instrument)
    simulator = TradeSimulator(config, clock)

    setup_rows: list[dict] = []
    trade_rows: list[dict] = []
    event_rows: list[dict] = []
    contract_rows: list[dict] = []
    results: list[SessionResult] = []

    sessions = sorted(
        d
        for d in bars["globex_session_date"].unique().to_list()
        if start_d <= d <= end_d and calendar.is_trading_day(d)
    )
    prev_session_bars: pl.DataFrame | None = None

    for session_date in sessions:
        session_bars = bars.filter(pl.col("globex_session_date") == session_date).sort(
            "timestamp_utc"
        )
        if session_bars.height < 5:
            continue
        session_bars = add_indicators(session_bars, config.atr.length, config.atr.method)
        finer = _finer_index(finer_bars, session_date) if finer_bars is not None else None

        result = simulator.run_session(session_bars, session_date, finer=finer)
        results.append(result)
        contract_rows.append(
            {
                "session_date": session_date,
                "underlying_contract": result.contract,
                "symbol": result.symbol,
                "roll_method": session_bars["roll_method"][0]
                if "roll_method" in session_bars.columns
                else None,
                "days_to_expiration": session_bars["days_to_expiration"][0]
                if "days_to_expiration" in session_bars.columns
                else None,
                "roll_period": session_bars["roll_period"][0]
                if "roll_period" in session_bars.columns
                else None,
                "bars": session_bars.height,
                "instrument": config.instrument,
                "contract_mode": config.contract_mode,
            }
        )
        event_rows.extend(result.events)

        features = build_setup_features(
            result, session_bars, prev_session_bars, clock, config, costs
        )
        for cand in result.candidates:
            row = cand.to_row()
            row.update(
                {
                    "run_instrument": config.instrument,
                    "underlying_contract": result.contract,
                    "contract_mode": config.contract_mode,
                    "roll_period": contract_rows[-1]["roll_period"],
                    "days_to_expiration": contract_rows[-1]["days_to_expiration"],
                }
            )
            if cand.selected:
                row.update(features)
            setup_rows.append(row)

        for trade in result.trades:
            _attach_zone_context(trade, result)
            metrics = compute_trade_metrics(trade, costs, config.labels, config.orders.quantity)
            # setup-level features first, then everything specific to *this*
            # order — an inversion trade must never inherit the original
            # order's target, entry or timings
            row = {
                **{k: v for k, v in features.items() if k != "session_date"},
                **_order_target_features(trade, costs, config),
                "session_date": session_date,
                "underlying_contract": result.contract,
                "run_instrument": config.instrument,
                "symbol": result.symbol,
                "contract_mode": config.contract_mode,
                "roll_period": contract_rows[-1]["roll_period"],
                "days_to_expiration": contract_rows[-1]["days_to_expiration"],
                "entry_time": trade.entry_time,
                "entry_fill": trade.entry_price,
                "exit_time": trade.exit_time,
                "exit_fill": trade.exit_price,
                "entry_delay_minutes": int(
                    (trade.entry_time - result.selected.c3_time).total_seconds() // 60
                ),
                **trade.order.to_dict(costs.tick),
                **metrics,
            }
            trade_rows.append(row)

        if result.selected is not None:
            onset = range_onset(
                session_bars.to_dicts(), result.selected.c3_index, config.range_research
            )
            setup_rows[-1].update(onset) if setup_rows else None
            for r in setup_rows:
                if r.get("selected") and r.get("session_date") == session_date:
                    r.update(onset)
        prev_session_bars = session_bars

    setups = _frame(setup_rows)
    trades = _frame(trade_rows)
    events = _frame(event_rows)
    contracts = _frame(contract_rows)
    daily = _daily_results(trades)
    summary = _summarize(config, sessions, setups, trades, events, quality)

    return RunOutput(
        run_id=run_id or new_run_id(),
        config=config,
        setups=setups,
        trades=trades,
        events=events,
        contracts=contracts,
        daily=daily,
        summary=summary,
        quality=quality,
        session_results=results,
        bars=bars if keep_bars else None,
    )


def _finer_index(finer: pl.DataFrame, session_date: date) -> dict:
    part = finer.filter(pl.col("globex_session_date") == session_date)
    out: dict = {}
    for row in part.iter_rows(named=True):
        minute = row["timestamp_utc"].replace(second=0, microsecond=0)
        out.setdefault(minute, []).append(row)
    return out


def _order_target_features(trade, costs: CostModel, config: AppConfig) -> dict:
    """Target metrics for the order this trade actually filled."""
    sel = trade.order.context.get("target_selection")
    if sel is None:
        return {}
    atr = trade.order.context.get("atr_at_formation") or 0.0
    return sel.to_dict(
        now=trade.entry_time,
        entry=trade.order.entry,
        tick=costs.tick,
        atr=atr,
        risk=trade.order.risk_points,
    )


def _attach_zone_context(trade, result: SessionResult) -> None:
    zone = result.zone
    if zone is None:
        return
    trade.order.context.update(
        {
            "zone_low": zone.low,
            "zone_high": zone.high,
            "zone_midpoint": zone.midpoint,
            "max_penetration": zone.max_penetration,
            "atr_at_formation": zone.fvg.atr_at_formation,
        }
    )


def _frame(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    normalized = [{k: r.get(k) for k in keys} for r in rows]
    return pl.DataFrame(normalized, strict=False, infer_schema_length=None)


def _daily_results(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    return (
        trades.group_by("session_date")
        .agg(
            pl.len().alias("trades"),
            pl.col("result_r").sum().alias("total_r"),
            pl.col("net_result_r").sum().alias("net_total_r"),
            pl.col("net_dollars").sum().alias("net_dollars"),
            pl.col("win").sum().alias("wins"),
            pl.col("is_clean_win").sum().alias("clean_wins"),
            pl.col("is_sweaty_win").sum().alias("sweaty_wins"),
            pl.col("is_ranging").sum().alias("ranging"),
            pl.col("underlying_contract").first().alias("underlying_contract"),
        )
        .sort("session_date")
        .with_columns(
            pl.col("total_r").cum_sum().alias("cumulative_r"),
            pl.col("net_total_r").cum_sum().alias("cumulative_net_r"),
            pl.col("net_dollars").cum_sum().alias("cumulative_net_dollars"),
        )
        .with_columns(
            (pl.col("cumulative_r") - pl.col("cumulative_r").cum_max()).alias("drawdown_r")
        )
    )


def _summarize(config, sessions, setups, trades, events, quality) -> dict:
    out = {
        "instrument": config.instrument,
        "contract_mode": config.contract_mode,
        "contract": config.contract,
        "start": config.start,
        "end": config.end,
        "execution_mode": config.execution.mode,
        "inversion_stop_model": config.inversion.stop_model,
        "entry_model": config.entries.model,
        "sessions_processed": len(sessions),
        "candidate_fvgs": setups.height,
        "qualifying_fvgs": int(setups["selected"].sum()) if not setups.is_empty() else 0,
        "rejected_candidates": (
            int((~setups["selected"]).sum()) if not setups.is_empty() else 0
        ),
        "trades": trades.height,
    }
    if not setups.is_empty():
        sel = setups.filter(pl.col("selected"))
        out["significance_breakdown"] = (
            sel["significance_type"].value_counts().to_dicts() if not sel.is_empty() else []
        )
    if not events.is_empty() and "event" in events.columns:
        out["event_counts"] = events["event"].value_counts().to_dicts()
        out["ambiguous_execution_events"] = int(
            (events["event"] == "AMBIGUOUS_SEQUENCE").sum()
        )
    if not trades.is_empty():
        out.update(
            {
                "original_trades": int((trades["order_kind"] == "ORIGINAL").sum()),
                "inversion_trades": int((trades["order_kind"] == "INVERSION").sum()),
                "reinversion_trades": int((trades["order_kind"] == "REINVERSION").sum()),
                "expectancy_r": float(trades["result_r"].mean()),
                "net_expectancy_r": float(trades["net_result_r"].mean()),
                "win_rate": float(trades["win"].mean()),
                "clean_win_rate": float(trades["is_clean_win"].mean()),
                "sweaty_win_rate": float(trades["is_sweaty_win"].mean()),
                "ranging_rate": float(trades["is_ranging"].mean()),
                "total_r": float(trades["result_r"].sum()),
                "net_dollars": float(trades["net_dollars"].sum()),
                "median_mae_r": float(trades["mae_r"].median()),
                "median_mfe_r": float(trades["mfe_r"].median()),
            }
        )
    if quality:
        out["data_quality"] = quality.to_dict()
    return out


def render_report(run: RunOutput) -> str:
    """Self-contained HTML summary of the run."""
    s = run.summary
    rows = "".join(
        f"<tr><th>{k}</th><td><pre>{json.dumps(v, indent=2, default=str)}</pre></td></tr>"
        if isinstance(v, (dict, list))
        else f"<tr><th>{k}</th><td>{v}</td></tr>"
        for k, v in s.items()
    )
    daily_html = (
        run.daily.to_pandas().to_html(index=False, border=0)
        if not run.daily.is_empty()
        else "<p>No trades.</p>"
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>FVG run {run.run_id}</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; max-width: 70rem; }}
 table {{ border-collapse: collapse; margin: 1rem 0; width: 100%; }}
 th, td {{ text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #ddd;
          vertical-align: top; font-size: 0.9rem; }}
 th {{ white-space: nowrap; }}
 pre {{ margin: 0; font-size: 0.8rem; }}
 caption {{ text-align: left; font-weight: 600; padding-bottom: 0.4rem; }}
</style></head><body>
<h1>First Presented FVG — run {run.run_id}</h1>
<p><strong>Historical research only.</strong> This report describes simulated
trades on past data; no orders were placed.</p>
<table><caption>Summary</caption>{rows}</table>
<h2>Daily results</h2>
{daily_html}
</body></html>"""
