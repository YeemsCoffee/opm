"""Streamlit research dashboard.

    streamlit run app.py

Nine pages: Data, Strategy settings, Backtest, Results, Conditions explorer,
Trade explorer, Range analysis, Walk-forward and Predictive models.
Everything the CLI can do is reachable here without editing Python.

This is a research surface over historical data — it never places orders.
"""

from __future__ import annotations

from datetime import date, timedelta, timezone

import polars as pl
import streamlit as st
import yaml

from ..analytics.stats import KEY_STAT_COLUMNS, conditional_table, summarize_trades
from ..analytics.walkforward import consistency_report
from ..config.loader import config_to_yaml
from ..config.schema import AppConfig
from ..data.cache import DataCache
from ..futures.contracts import contract_expiration, list_contracts
from ..liquidity.pivots import PivotTracker
from ..pipeline import load_bars, make_provider, run_backtest
from ..sessions.calendar import TradingCalendar
from ..sessions.clock import SessionClock
from ..visualization.charts import (
    conditional_bar_chart,
    drawdown_chart,
    equity_curve,
    monthly_bar_chart,
    range_probability_chart,
    trade_chart,
)
from .state import (
    DASHBOARD_PAGES,
    categorical_columns,
    default_config,
    list_runs,
    load_run,
    numeric_columns,
    session_frames,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def _config() -> AppConfig:
    if "config" not in st.session_state:
        st.session_state.config = default_config()
    return st.session_state.config


def _clock(config: AppConfig) -> SessionClock:
    return SessionClock(config=config.sessions, calendar=TradingCalendar())


def _current_run():
    return st.session_state.get("run")


def _show_stats(df: pl.DataFrame, narrow: bool = True) -> None:
    if df.is_empty():
        st.info("No trades in this selection.")
        return
    cols = [c for c in KEY_STAT_COLUMNS if c in df.columns] if narrow else df.columns
    st.dataframe(df.select(cols).to_pandas(), use_container_width=True, hide_index=True)


def _download_button(df: pl.DataFrame, label: str, filename: str) -> None:
    if df.is_empty():
        return
    st.download_button(
        label, df.write_csv().encode(), file_name=filename, mime="text/csv"
    )


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def page_data() -> None:
    config = _config()
    st.header("Data")
    st.caption(
        "Pick the instrument and contract mode, check the Databento credential, "
        "download or import candles, and review data quality."
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        config.instrument = st.selectbox(
            "Instrument", ["NQ", "MNQ"], index=["NQ", "MNQ"].index(config.instrument)
        )
    with col2:
        config.contract_mode = st.radio(
            "Contract mode", ["DATED", "CONTINUOUS"],
            index=["DATED", "CONTINUOUS"].index(config.contract_mode),
            help=(
                "DATED runs one real contract — the most reliable mode for "
                "execution testing. CONTINUOUS stitches contracts with an "
                "explicit roll rule and never back-adjusts by default."
            ),
        )
    with col3:
        config.data.provider = st.selectbox(
            "Provider", ["databento", "csv", "parquet", "synthetic"],
            index=["databento", "csv", "parquet", "synthetic"].index(config.data.provider),
        )

    instrument = config.instruments[config.instrument]
    today = date.today()
    contracts = list_contracts(instrument, today - timedelta(days=400), today + timedelta(days=200))
    codes = [c.code for c in contracts]
    if config.contract_mode == "DATED":
        current = config.contract if config.contract in codes else (codes[0] if codes else None)
        config.contract = st.selectbox(
            "Contract", codes, index=codes.index(current) if current in codes else 0
        )
    else:
        config.contract = None
        st.selectbox(
            "Roll method", ["HIGHEST_VOLUME", "FIXED_DAYS_BEFORE_EXPIRATION",
                            "USER_DEFINED_ROLL_CALENDAR"],
            index=["HIGHEST_VOLUME", "FIXED_DAYS_BEFORE_EXPIRATION",
                   "USER_DEFINED_ROLL_CALENDAR"].index(config.rolls.method),
            key="roll_method_select",
            on_change=lambda: setattr(
                config.rolls, "method", st.session_state.roll_method_select
            ),
        )

    c1, c2, c3 = st.columns(3)
    with c1:
        start = st.date_input(
            "Start", value=date.fromisoformat(config.start) if config.start else date(2025, 1, 6)
        )
    with c2:
        end = st.date_input(
            "End", value=date.fromisoformat(config.end) if config.end else date(2025, 3, 28)
        )
    with c3:
        config.data.execution_resolution = st.selectbox(
            "Execution resolution", ["1m", "1s", "tick"],
            index=["1m", "1s", "tick"].index(config.data.execution_resolution),
        )
    config.start, config.end = start.isoformat(), end.isoformat()

    if config.data.provider in ("csv", "parquet"):
        config.data.path = st.text_input(
            "File or directory", value=config.data.path or "sample_data/NQ_1m_2025Q1.parquet"
        )

    st.subheader("Databento credential")
    from ..data.databento_provider import credential_status

    status = credential_status()
    if status["present"] and status["valid_format"]:
        st.success(f"DATABENTO_API_KEY detected ({status['masked']})")
    elif status["present"]:
        st.warning(f"Key found but {status['hint']}")
    else:
        st.info(
            "DATABENTO_API_KEY is not set. Copy .env.example to .env and add your "
            "key, or use the csv/parquet provider offline."
        )

    st.subheader("Contracts")
    st.dataframe(
        pl.DataFrame([
            {
                "contract": c.code,
                "databento_symbol": c.databento_raw,
                "expiration": contract_expiration(c, instrument),
                "tick_size": instrument.tick_size,
                "point_value": instrument.point_value,
                "tick_value": instrument.tick_value,
            }
            for c in contracts
        ]).to_pandas(),
        use_container_width=True, hide_index=True,
    )

    if st.button("Load / download data", type="primary"):
        with st.spinner("loading candles…"):
            try:
                clock = _clock(config)
                bars, quality = load_bars(config, clock, make_provider(config, clock))
                st.session_state.bars = bars
                st.session_state.quality = quality
                st.success(f"{bars.height:,} bars loaded")
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                st.error(f"{type(exc).__name__}: {exc}")

    bars = st.session_state.get("bars")
    if bars is not None and not bars.is_empty():
        st.subheader("Loaded data")
        st.dataframe(
            bars.group_by("underlying_contract")
            .agg(
                pl.len().alias("bars"),
                pl.col("globex_session_date").min().alias("from"),
                pl.col("globex_session_date").max().alias("to"),
            )
            .sort("from")
            .to_pandas(),
            use_container_width=True, hide_index=True,
        )
        if "roll_period" in bars.columns:
            st.caption("Roll calendar")
            st.dataframe(
                bars.group_by(["underlying_contract", "roll_period"])
                .agg(pl.col("globex_session_date").n_unique().alias("sessions"))
                .sort(["underlying_contract", "roll_period"])
                .to_pandas(),
                use_container_width=True, hide_index=True,
            )

    quality = st.session_state.get("quality")
    if quality is not None:
        st.subheader("Data-quality report")
        if quality.ok:
            st.success("No blocking errors.")
        else:
            st.error(f"{len(quality.errors)} error(s) found.")
        if quality.issues:
            st.dataframe(
                pl.DataFrame([i.to_dict() for i in quality.issues]).to_pandas(),
                use_container_width=True, hide_index=True,
            )

    st.subheader("Cache inventory")
    inv = DataCache(config.data.cache_dir).inventory_frame()
    if inv.is_empty():
        st.caption(f"Cache `{config.data.cache_dir}` is empty.")
    else:
        st.dataframe(inv.to_pandas(), use_container_width=True, hide_index=True)


def page_settings() -> None:
    config = _config()
    st.header("Strategy settings")
    st.caption("Every rule and threshold. Import or export the whole thing as YAML.")

    tabs = st.tabs([
        "Sessions", "FVG & significance", "Liquidity & targets",
        "Entries & inversion", "Execution & costs", "Labels", "YAML",
    ])

    with tabs[0]:
        s = config.sessions
        c1, c2, c3 = st.columns(3)
        s.cash_open = c1.text_input("Cash open", s.cash_open)
        s.cash_close = c2.text_input("Cash close", s.cash_close)
        s.globex_session_start = c3.text_input("Globex start", s.globex_session_start)
        s.fvg_search_start = c1.text_input("FVG search start", s.fvg_search_start)
        s.fvg_search_end = c2.text_input("FVG search end", s.fvg_search_end)
        s.trade_management_end = c3.text_input("Management end", s.trade_management_end)
        s.include_globex_data_for_indicators = st.checkbox(
            "Use Globex data for indicators", s.include_globex_data_for_indicators
        )
        s.include_premarket_for_liquidity = st.checkbox(
            "Use premarket for liquidity", s.include_premarket_for_liquidity
        )

    with tabs[1]:
        c1, c2 = st.columns(2)
        config.atr.length = c1.number_input("ATR length", 2, 500, config.atr.length)
        config.atr.method = c2.selectbox(
            "ATR method", ["WILDER", "SMA"], index=["WILDER", "SMA"].index(config.atr.method)
        )
        config.fvg.all_candles_after_open = st.checkbox(
            "All three candles must start at/after the search start",
            config.fvg.all_candles_after_open,
            help="Off: only candle 3 must complete after the open.",
        )
        config.fvg.strict_inequality = st.checkbox(
            "Strict inequality (a touch is not a gap)", config.fvg.strict_inequality
        )
        st.markdown("**Type A** — size and preservation")
        a1, a2 = st.columns(2)
        config.significance.type_a.minimum_gap_atr = a1.number_input(
            "Minimum gap (ATR)", 0.0, 5.0, config.significance.type_a.minimum_gap_atr, 0.01
        )
        config.significance.type_a.minimum_preservation_ratio = a2.number_input(
            "Minimum preservation ratio", 0.0, 1.0,
            config.significance.type_a.minimum_preservation_ratio, 0.05
        )
        st.markdown("**Type B** — prior wick into the gap")
        b = config.significance.type_b
        b1, b2, b3, b4 = st.columns(4)
        b.prior_wick_lookback_minutes = b1.number_input(
            "Wick lookback (min)", 1, 120, b.prior_wick_lookback_minutes
        )
        b.minimum_wick_atr = b2.number_input("Min wick (ATR)", 0.0, 5.0, b.minimum_wick_atr, 0.01)
        b.minimum_wick_share = b3.number_input("Min wick share", 0.0, 1.0, b.minimum_wick_share, 0.05)
        b.minimum_fvg_overlap_ratio = b4.number_input(
            "Min overlap ratio", 0.0, 1.0, b.minimum_fvg_overlap_ratio, 0.05
        )
        config.zone.invert_on_touch_close = st.checkbox(
            "A close exactly on the boundary inverts the zone",
            config.zone.invert_on_touch_close,
        )

    with tabs[2]:
        liq, tgt, eq = config.liquidity, config.targets, config.equal_levels
        c1, c2, c3 = st.columns(3)
        liq.lookback_minutes = c1.number_input("Liquidity lookback (min)", 5, 480, liq.lookback_minutes)
        liq.pivot_strength = c2.selectbox(
            "Pivot strength", [1, 2, 3], index=[1, 2, 3].index(liq.pivot_strength),
            format_func=lambda k: f"{k}x{k}",
        )
        liq.sweep_tolerance_ticks = c3.number_input(
            "Sweep tolerance (ticks)", 0.0, 40.0, liq.sweep_tolerance_ticks, 1.0
        )
        liq.count_exact_touch_as_sweep = st.checkbox(
            "An exact touch counts as a sweep", liq.count_exact_touch_as_sweep
        )
        t1, t2 = st.columns(2)
        tgt.min_target_age_minutes = t1.number_input(
            "Minimum target age (min)", 0, 120, tgt.min_target_age_minutes
        )
        tgt.max_lookback_minutes = t2.number_input(
            "Target lookback (min)", 5, 480, tgt.max_lookback_minutes
        )
        tgt.allow_touched_targets = st.checkbox(
            "Touched-but-not-swept levels stay eligible", tgt.allow_touched_targets
        )
        e1, e2 = st.columns(2)
        eq.tolerance_mode = e1.selectbox(
            "Equal-level tolerance", ["ticks", "atr"],
            index=["ticks", "atr"].index(eq.tolerance_mode)
        )
        if eq.tolerance_mode == "ticks":
            eq.tolerance_ticks = e2.number_input("Tolerance (ticks)", 0.0, 20.0, eq.tolerance_ticks, 1.0)
        else:
            eq.tolerance_atr = e2.number_input("Tolerance (ATR)", 0.0, 1.0, eq.tolerance_atr, 0.01)

    with tabs[3]:
        models = ["PROXIMAL_EDGE", "MIDPOINT", "DISTAL_EDGE"]
        c1, c2 = st.columns(2)
        config.entries.model = c1.selectbox(
            "Original entry model", models, index=models.index(config.entries.model)
        )
        config.inversion.entry_model = c2.selectbox(
            "Inversion entry model", models, index=models.index(config.inversion.entry_model)
        )
        stops = [
            "OPPOSITE_FVG_EDGE_PLUS_BUFFER", "INVERSION_CANDLE_EXTREME_PLUS_BUFFER",
            "MOST_RECENT_SWING_PLUS_BUFFER", "ORIGINAL_CANDLE_1_EXTREME",
        ]
        config.inversion.stop_model = st.selectbox(
            "Inversion stop model", stops, index=stops.index(config.inversion.stop_model),
            help="Results from different stop models are reported separately, never pooled.",
        )
        buf = config.inversion.stop_buffer
        u1, u2 = st.columns(2)
        buf.unit = u1.selectbox(
            "Stop buffer unit", ["ticks", "points", "atr"],
            index=["ticks", "points", "atr"].index(buf.unit)
        )
        if buf.unit == "ticks":
            buf.ticks = u2.number_input("Buffer (ticks)", 0.0, 100.0, buf.ticks, 1.0)
        elif buf.unit == "points":
            buf.points = u2.number_input("Buffer (points)", 0.0, 100.0, buf.points, 0.25)
        else:
            buf.atr = u2.number_input("Buffer (ATR)", 0.0, 3.0, buf.atr, 0.05)
        config.inversion.enabled = st.checkbox("Trade inversions", config.inversion.enabled)
        config.orders.max_order_age_minutes = st.number_input(
            "Order expiration (minutes)", 1, 390, config.orders.max_order_age_minutes
        )
        config.orders.cancel_when_target_swept = st.checkbox(
            "Cancel when the target is swept before entry", config.orders.cancel_when_target_swept
        )

    with tabs[4]:
        modes = ["ONE_MINUTE_CONSERVATIVE", "ONE_SECOND_INTRABAR", "TICK_INTRABAR"]
        config.execution.mode = st.selectbox(
            "Execution mode", modes, index=modes.index(config.execution.mode),
            help=(
                "Conservative assumes the adverse order when a single minute "
                "touches several levels, and logs the ambiguity."
            ),
        )
        for root in ("NQ", "MNQ"):
            st.markdown(f"**{root} costs**")
            costs = config.instruments[root].costs
            c1, c2, c3 = st.columns(3)
            costs.commission_per_contract = c1.number_input(
                f"{root} commission / side", 0.0, 20.0, costs.commission_per_contract, 0.05,
                key=f"{root}_comm",
            )
            costs.exchange_fees_per_contract = c2.number_input(
                f"{root} exchange fees / side", 0.0, 20.0, costs.exchange_fees_per_contract, 0.01,
                key=f"{root}_fees",
            )
            costs.spread_ticks = c3.number_input(
                f"{root} spread (ticks)", 0.0, 20.0, costs.spread_ticks, 0.5, key=f"{root}_spread",
            )
            s1, s2 = st.columns(2)
            costs.entry_slippage_ticks = s1.number_input(
                f"{root} entry slippage (ticks)", 0.0, 20.0, costs.entry_slippage_ticks, 0.5,
                key=f"{root}_entry_slip",
            )
            costs.stop_slippage_ticks = s2.number_input(
                f"{root} stop slippage (ticks)", 0.0, 20.0, costs.stop_slippage_ticks, 0.5,
                key=f"{root}_stop_slip",
            )

    with tabs[5]:
        labels = config.labels
        st.markdown("**Clean win**")
        c1, c2, c3, c4 = st.columns(4)
        labels.clean_win.max_mae_r = c1.number_input(
            "Max MAE (R)", 0.0, 2.0, labels.clean_win.max_mae_r, 0.05
        )
        labels.clean_win.max_entry_recross = c2.number_input(
            "Max entry recrosses", 0, 20, labels.clean_win.max_entry_recross
        )
        labels.clean_win.reach_half_r_within_minutes = c3.number_input(
            "0.5R within (min)", 1, 120, labels.clean_win.reach_half_r_within_minutes
        )
        labels.clean_win.max_duration_minutes = c4.number_input(
            "Max duration (min)", 1, 390, labels.clean_win.max_duration_minutes
        )
        st.markdown("**Sweaty win** — a win meeting at least N stress conditions")
        s1, s2, s3 = st.columns(3)
        labels.sweaty_win.min_conditions = s1.number_input(
            "Conditions required", 1, 7, labels.sweaty_win.min_conditions
        )
        labels.sweaty_win.mae_r_above = s2.number_input(
            "MAE above (R)", 0.0, 2.0, labels.sweaty_win.mae_r_above, 0.05
        )
        labels.sweaty_win.duration_over_minutes = s3.number_input(
            "Duration over (min)", 1, 390, labels.sweaty_win.duration_over_minutes
        )
        st.markdown("**Ranging**")
        r1, r2, r3 = st.columns(3)
        labels.ranging.min_conditions = r1.number_input(
            "Conditions required ", 1, 7, labels.ranging.min_conditions
        )
        labels.ranging.efficiency_ratio_below = r2.number_input(
            "Efficiency ratio below", 0.0, 1.0, labels.ranging.efficiency_ratio_below, 0.05
        )
        labels.ranging.overlap_above = r3.number_input(
            "Candle overlap above", 0.0, 1.0, labels.ranging.overlap_above, 0.05
        )

    with tabs[6]:
        st.download_button(
            "Export YAML", config_to_yaml(config).encode(),
            file_name="fvg_config.yaml", mime="text/yaml",
        )
        uploaded = st.file_uploader("Import YAML", type=["yaml", "yml"])
        if uploaded is not None and st.button("Apply uploaded YAML"):
            try:
                data = yaml.safe_load(uploaded.getvalue().decode()) or {}
                st.session_state.config = AppConfig.model_validate(data)
                st.success("Configuration replaced.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"could not apply: {exc}")
        st.code(config_to_yaml(config), language="yaml")


def page_backtest() -> None:
    config = _config()
    st.header("Backtest")
    st.caption("Run the strategy over the loaded range and inspect what happened.")

    c1, c2 = st.columns([1, 2])
    with c1:
        run_id = st.text_input("Run ID (blank = auto)", "")
        if st.button("Run backtest", type="primary"):
            with st.spinner("running…"):
                try:
                    bars = st.session_state.get("bars")
                    out = run_backtest(config, bars=bars, run_id=run_id or None)
                    out.write(config.runs_dir)
                    st.session_state.run = out
                    st.success(f"Run {out.run_id} complete — written to {config.runs_dir}")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"{type(exc).__name__}: {exc}")
    with c2:
        runs = list_runs(config.runs_dir)
        if runs:
            chosen = st.selectbox("…or load a stored run", runs)
            if st.button("Load run"):
                st.session_state.run = load_run(chosen, config.runs_dir)
                st.success(f"Loaded {chosen}")

    run = _current_run()
    if run is None:
        st.info("No run loaded yet.")
        return

    summary = run.summary
    m = st.columns(5)
    m[0].metric("Sessions", summary.get("sessions_processed", 0))
    m[1].metric("Candidate FVGs", summary.get("candidate_fvgs", 0))
    m[2].metric("Qualifying", summary.get("qualifying_fvgs", 0))
    m[3].metric("Trades", summary.get("trades", 0))
    m[4].metric("Ambiguous fills", summary.get("ambiguous_execution_events", 0))

    m2 = st.columns(4)
    m2[0].metric("Original", summary.get("original_trades", 0))
    m2[1].metric("Inversion", summary.get("inversion_trades", 0))
    m2[2].metric("Re-inversion", summary.get("reinversion_trades", 0))
    m2[3].metric("Rejected candidates", summary.get("rejected_candidates", 0))

    if summary.get("significance_breakdown"):
        st.subheader("Type classification")
        st.dataframe(
            pl.DataFrame(summary["significance_breakdown"]).to_pandas(),
            use_container_width=True, hide_index=True,
        )

    setups = run.setups
    if not setups.is_empty():
        st.subheader("Setups (including rejected candidates)")
        cols = [
            c for c in (
                "session_date", "c3_time", "direction", "significance_type", "selected",
                "rejection_reason", "gap_width", "type_a_normalized_gap",
                "type_a_preservation_ratio", "type_b_qualifying_count",
                "target_found", "target_price", "target_distance_r",
            ) if c in setups.columns
        ]
        st.dataframe(setups.select(cols).to_pandas(), use_container_width=True, hide_index=True)
        _download_button(setups, "Export setups CSV", "setups.csv")

    if not run.events.is_empty():
        st.subheader("Event log")
        counts = run.events["event"].value_counts().sort("count", descending=True)
        st.dataframe(counts.to_pandas(), use_container_width=True, hide_index=True)
        kinds = st.multiselect(
            "Filter events", counts["event"].to_list(), default=[]
        )
        shown = run.events.filter(pl.col("event").is_in(kinds)) if kinds else run.events
        st.dataframe(shown.head(500).to_pandas(), use_container_width=True, hide_index=True)
        _download_button(run.events, "Export events CSV", "events.csv")

    warnings = [
        w for r in getattr(run, "session_results", []) or [] for w in getattr(r, "warnings", [])
    ]
    if warnings:
        st.subheader("Data warnings")
        for w in warnings[:50]:
            st.warning(w)


def page_results() -> None:
    run = _current_run()
    st.header("Results")
    if run is None or run.trades.is_empty():
        st.info("Run or load a backtest with at least one trade.")
        return
    trades, daily = run.trades, run.daily

    stats = summarize_trades(trades, group=run.summary.get("instrument", "ALL"))
    m = st.columns(6)
    m[0].metric("Trades", stats["trades"])
    m[1].metric("Expectancy (R)", f"{stats['expectancy_r']:.3f}")
    m[2].metric("Net expectancy (R)", f"{stats['net_expectancy_r']:.3f}")
    m[3].metric("Win rate", f"{stats['win_rate']:.1%}")
    m[4].metric("Clean wins", f"{(stats['clean_win_rate'] or 0):.1%}")
    m[5].metric("Sweaty wins", f"{(stats['sweaty_win_rate'] or 0):.1%}")
    if not stats["reliable"]:
        st.warning(stats["warning"])

    st.plotly_chart(equity_curve(daily, in_dollars=True), use_container_width=True)
    st.plotly_chart(drawdown_chart(daily), use_container_width=True)
    st.plotly_chart(monthly_bar_chart(trades), use_container_width=True)

    st.subheader("Original vs inversion")
    _show_stats(conditional_table(trades, "order_kind"))

    if "direction" in trades.columns:
        st.subheader("Long vs short")
        _show_stats(conditional_table(trades, "direction"))

    st.subheader("Compare instruments")
    st.caption(
        "Load a run per instrument (Backtest page) — NQ and MNQ are always "
        "reported separately."
    )
    other = st.selectbox("Second run", ["(none)"] + list_runs(_config().runs_dir))
    if other != "(none)":
        second = load_run(other, _config().runs_dir)
        if not second.trades.is_empty():
            from ..analytics.stats import compare_instruments, normalized_comparison

            frames = {
                f"{run.summary.get('instrument')} ({run.run_id})": trades,
                f"{second.summary.get('instrument')} ({second.run_id})": second.trades,
            }
            _show_stats(compare_instruments(frames))
            st.caption("Same sessions only")
            _show_stats(normalized_comparison(frames))

    if "underlying_contract" in trades.columns:
        st.subheader("Contract-to-contract consistency")
        cons = consistency_report(trades)
        if not cons.is_empty():
            _show_stats(cons)
            if bool(cons["inconsistent"][0]):
                st.warning(
                    "Results are not consistent across contracts — treat the "
                    "aggregate expectancy with suspicion."
                )

    _download_button(trades, "Export trades CSV", "trades.csv")


def page_conditions() -> None:
    run = _current_run()
    st.header("Conditions explorer")
    if run is None or run.trades.is_empty():
        st.info("Run or load a backtest with at least one trade.")
        return
    trades = run.trades

    cats = categorical_columns(trades)
    nums = numeric_columns(trades)
    kind = st.radio("Feature type", ["categorical", "numeric"], horizontal=True)

    if kind == "categorical":
        default = "significance_type" if "significance_type" in cats else cats[0]
        column = st.selectbox("Feature", cats, index=cats.index(default))
        table = conditional_table(trades, column, min_sample=st.session_state.get("min_sample", 20))
    else:
        default = "target_distance_r" if "target_distance_r" in nums else nums[0]
        column = st.selectbox("Feature", nums, index=nums.index(default))
        mode = st.radio("Binning", ["quantiles", "custom edges"], horizontal=True)
        if mode == "quantiles":
            n = st.slider("Number of bins", 2, 8, 4)
            table = conditional_table(trades, column, bins=n)
        else:
            raw = st.text_input("Bin edges (comma separated)", "0.5, 1.0, 2.0")
            try:
                edges = [float(x) for x in raw.split(",") if x.strip()]
            except ValueError:
                st.error("Could not parse the edges.")
                return
            table = conditional_table(trades, column, bins=edges)

    metric = st.selectbox(
        "Metric",
        ["expectancy_r", "net_expectancy_r", "win_rate", "median_mae_r",
         "median_mfe_r", "clean_win_rate", "sweaty_win_rate", "ranging_rate"],
    )
    st.plotly_chart(
        conditional_bar_chart(table, metric, f"{metric} by {column}"),
        use_container_width=True,
    )
    st.caption("Grey bars are small samples — they are not ranked as reliable.")
    _show_stats(table)
    _download_button(table, "Export table CSV", f"conditions_{column}.csv")


def page_trade_explorer() -> None:
    run = _current_run()
    config = _config()
    st.header("Trade explorer")
    if run is None:
        st.info("Run or load a backtest first.")
        return
    sessions = run.sessions() if hasattr(run, "sessions") else sorted(
        run.setups["session_date"].unique().to_list()
    ) if not run.setups.is_empty() else []
    if not sessions:
        st.info("This run has no setups to inspect.")
        return

    session_date = st.selectbox("Session", sessions, index=len(sessions) - 1)
    setups = run.setups.filter(
        (pl.col("session_date") == session_date) & pl.col("selected")
    )
    setup = setups.to_dicts()[0] if not setups.is_empty() else None
    trades = (
        run.trades.filter(pl.col("session_date") == session_date).to_dicts()
        if not run.trades.is_empty() else []
    )
    events = (
        run.events.filter(pl.col("session_date") == session_date)
        if not run.events.is_empty() else pl.DataFrame()
    )

    bars = st.session_state.get("bars")
    if bars is None:
        st.warning(
            "Load candles on the Data page to draw the chart — the run files "
            "store results, not raw bars."
        )
    else:
        session_bars = session_frames(bars, session_date)
        cash = session_bars.filter(
            pl.col("timestamp_ny") >= _clock(config).ny_datetime(session_date, "08:00")
        )
        pivots = []
        if not session_bars.is_empty():
            tracker = PivotTracker(config.liquidity, tick_size=config.active_instrument.tick_size)
            for bar in session_bars.iter_rows(named=True):
                tracker.push(bar)
            pivots = [
                p.to_dict() for p in tracker.pivots
                if p.timestamp >= cash["timestamp_ny"].min()
            ] if not cash.is_empty() else []
        contract = setup.get("underlying_contract") if setup else ""
        st.plotly_chart(
            trade_chart(
                cash, setup=setup, trades=trades, events=events,
                context=setup or {}, pivots=pivots,
                title=f"{run.summary.get('instrument', '')} {contract} — {session_date}",
            ),
            use_container_width=True,
        )

    if setup:
        st.subheader("Why this FVG qualified")
        c1, c2, c3 = st.columns(3)
        c1.metric("Type", setup.get("significance_type") or "—")
        c1.metric("Gap (points)", f"{setup['gap_width']:.2f}")
        c1.metric("Gap (ATR)", f"{setup.get('type_a_normalized_gap', 0):.2f}")
        c2.metric("Preservation ratio", f"{setup.get('type_a_preservation_ratio', 0):.2f}")
        c2.metric("Body void", f"{setup.get('type_a_body_void', 0):.2f}")
        c2.metric("Qualifying wicks", setup.get("type_b_qualifying_count", 0))
        c3.metric("Zone touches", setup.get("zone_touch_count", 0))
        c3.metric("Inversions", setup.get("zone_inversion_count", 0))
        c3.metric("Closes inside", setup.get("zone_closes_inside", 0))
        if setup.get("type_b_closest_wick_timestamp") is not None:
            st.caption(
                f"Closest qualifying wick: {setup['type_b_closest_wick_side']} at "
                f"{setup['type_b_closest_wick_timestamp']}, "
                f"{setup.get('type_b_closest_wick_points', 0):.2f} points, "
                f"overlap {setup.get('type_b_closest_wick_overlap_ratio', 0):.0%} of the gap"
            )
        if setup.get("range_onset_found"):
            st.info(
                "Retrospective range onset "
                f"{setup['range_onset_minutes_after_formation']} minutes after "
                "formation (research label only — never an entry feature)."
            )

    if trades:
        st.subheader("Trades")
        cols = [
            c for c in (
                "order_kind", "direction", "entry_price", "stop_price", "target_price",
                "filled_at", "exit_time", "exit_reason", "result_r", "net_result_r",
                "mae_r", "mfe_r", "duration_minutes", "trade_label", "is_ranging",
                "ambiguous_execution",
            ) if c in run.trades.columns
        ]
        st.dataframe(
            pl.DataFrame(trades).select(cols).to_pandas(),
            use_container_width=True, hide_index=True,
        )
    if not events.is_empty():
        st.subheader("Event log for this session")
        st.dataframe(events.to_pandas(), use_container_width=True, hide_index=True)


def page_range_analysis() -> None:
    run = _current_run()
    st.header("Range analysis")
    st.caption(
        "Entry-time conditions associated with later ranging. Range-onset "
        "labels are retrospective and never used as features."
    )
    if run is None or run.trades.is_empty():
        st.info("Run or load a backtest with at least one trade.")
        return
    trades = run.trades

    rate = float(trades["is_ranging"].fill_null(False).cast(pl.Boolean).mean())
    st.metric("Ranging rate", f"{rate:.1%}")

    candidates = [
        ("entry_delay_minutes", 4),
        ("pre_entry_efficiency_10", 4),
        ("pre_entry_overlap", 4),
        ("fvg_gap_atr", 4),
        ("target_age_minutes", 4),
        ("target_distance_r", 4),
        ("opening_range_position", 4),
        ("overnight_range_position", 4),
        ("zone_before_entry_inversions", None),
        ("formation_hour", None),
    ]
    available = [(c, b) for c, b in candidates if c in trades.columns]
    if not available:
        st.info("This run has no range features.")
        return
    names = [c for c, _ in available]
    chosen = st.selectbox("Condition", names)
    bins = dict(available)[chosen]
    table = conditional_table(trades, chosen, bins=bins) if bins else conditional_table(trades, chosen)
    st.plotly_chart(range_probability_chart(table, chosen), use_container_width=True)
    _show_stats(table)

    st.subheader("All range conditions at a glance")
    rows = []
    for name, b in available:
        t = conditional_table(trades, name, bins=b) if b else conditional_table(trades, name)
        if t.is_empty() or "ranging_rate" not in t.columns:
            continue
        reliable = t.filter(pl.col("reliable"))
        use = reliable if not reliable.is_empty() else t
        rows.append(
            {
                "condition": name,
                "lowest_rate_group": use.sort("ranging_rate")["group"][0],
                "lowest_rate": use.sort("ranging_rate")["ranging_rate"][0],
                "highest_rate_group": use.sort("ranging_rate", descending=True)["group"][0],
                "highest_rate": use.sort("ranging_rate", descending=True)["ranging_rate"][0],
                "reliable_groups": int(t["reliable"].sum()),
            }
        )
    if rows:
        st.dataframe(pl.DataFrame(rows).to_pandas(), use_container_width=True, hide_index=True)


def page_walkforward() -> None:
    config = _config()
    st.header("Walk-forward")
    st.caption(
        "Thresholds are optimized inside the development window only, then "
        "frozen for validation and out-of-sample. Chronological by design — "
        "random splits leak the future into the past."
    )
    wf = config.walkforward
    c1, c2, c3, c4 = st.columns(4)
    wf.development_days = c1.number_input("Development (days)", 20, 2000, wf.development_days)
    wf.validation_days = c2.number_input("Validation (days)", 5, 500, wf.validation_days)
    wf.out_of_sample_days = c3.number_input("Out-of-sample (days)", 5, 500, wf.out_of_sample_days)
    wf.step_days = c4.number_input("Rolling step (days)", 5, 500, wf.step_days)
    wf.min_trades_per_fold = st.number_input(
        "Minimum trades per fold", 1, 500, wf.min_trades_per_fold
    )
    st.caption("Grid searched inside each development window:")
    st.json(wf.grid)

    if st.button("Run walk-forward", type="primary"):
        from ..analytics.walkforward import run_walkforward

        bars = st.session_state.get("bars")
        clock = _clock(config)
        provider = None if bars is not None else make_provider(config, clock)

        def run_fn(cfg, a, b):
            scoped = cfg.model_copy(deep=True)
            scoped.start, scoped.end = a.isoformat(), b.isoformat()
            try:
                return run_backtest(scoped, provider=provider, bars=bars).trades
            except Exception:  # noqa: BLE001 - a fold with no data is not fatal
                return pl.DataFrame()

        with st.spinner("walking forward…"):
            st.session_state.walkforward = run_walkforward(config, run_fn)

    result = st.session_state.get("walkforward")
    if result is None:
        st.info("No walk-forward result yet.")
        return
    for w in result.warnings:
        st.warning(w)
    if result.folds.is_empty():
        st.info("No fold produced enough trades.")
        return

    st.subheader("Folds")
    cols = [
        c for c in (
            "fold", "dev_start", "dev_end", "val_start", "oos_start", "oos_end",
            "parameters", "dev_trades", "dev_expectancy_r", "val_trades",
            "val_expectancy_r", "oos_trades", "oos_expectancy_r",
            "degradation_r", "holds_out_of_sample",
        ) if c in result.folds.columns
    ]
    st.dataframe(result.folds.select(cols).to_pandas(), use_container_width=True, hide_index=True)
    _download_button(result.folds, "Export folds CSV", "walkforward_folds.csv")

    held = int(result.folds["holds_out_of_sample"].sum())
    total = result.folds.height
    st.metric("Folds positive out of sample", f"{held} / {total}")
    if held < total:
        st.warning(
            "Filters that only work in their development window are the usual "
            "cause — compare the development and out-of-sample columns above."
        )


def page_models() -> None:
    run = _current_run()
    config = _config()
    st.header("Predictive models")
    st.warning(
        "These are **probabilities, not certainties**. Build conviction from "
        "the descriptive tables first; a model that disagrees with them is "
        "usually fitting noise."
    )
    if run is None or run.trades.is_empty():
        st.info("Run or load a backtest with at least one trade.")
        return

    from ..analytics.models import (
        TARGET_BUILDERS, results_frame, select_features, train_target,
    )

    features = select_features(run.trades)
    st.caption(
        f"{len(features)} entry-time features. Outcome columns, exit prices, "
        "MAE/MFE and retrospective range-onset labels are structurally excluded."
    )
    with st.expander("Feature list"):
        st.write(features)

    c1, c2, c3 = st.columns(3)
    target = c1.selectbox("Target", list(TARGET_BUILDERS))
    algorithm = c2.selectbox("Algorithm", ["logistic", "hist_gradient_boosting"])
    fraction = c3.slider("Chronological train fraction", 0.5, 0.9, config.models.train_fraction, 0.05)

    if st.button("Train", type="primary"):
        with st.spinner("training…"):
            st.session_state.model_result = train_target(
                run.trades, target, algorithm=algorithm,
                features=features, train_fraction=fraction,
            )

    result = st.session_state.get("model_result")
    if result is None:
        return
    if not result.trained:
        st.info(result.note or "Model not fitted.")
        return

    m = st.columns(5)
    m[0].metric("ROC-AUC", f"{result.roc_auc:.3f}" if result.roc_auc else "—")
    m[1].metric("Precision", f"{result.precision:.3f}")
    m[2].metric("Recall", f"{result.recall:.3f}")
    m[3].metric("Brier score", f"{result.brier:.3f}")
    m[4].metric("Validation trades", result.n_validation)
    if result.note:
        st.info(result.note)

    st.subheader("Calibration")
    st.caption("A usable model's observed rate tracks its predicted probability.")
    st.dataframe(
        pl.DataFrame(result.calibration).to_pandas(),
        use_container_width=True, hide_index=True,
    )

    st.subheader("Feature importance")
    st.dataframe(
        pl.DataFrame(result.feature_importance[:20]).to_pandas(),
        use_container_width=True, hide_index=True,
    )

    if result.partial_dependence:
        st.subheader("Partial dependence")
        pdp = pl.DataFrame(result.partial_dependence)
        import plotly.express as px

        st.plotly_chart(
            px.line(
                pdp.to_pandas(), x="value", y="mean_probability", color="feature",
                labels={"mean_probability": "predicted probability"},
            ),
            use_container_width=True,
        )

    _download_button(results_frame([result]), "Export model metrics CSV", "model.csv")


PAGE_FUNCS = {
    "Data": page_data,
    "Strategy settings": page_settings,
    "Backtest": page_backtest,
    "Results": page_results,
    "Conditions explorer": page_conditions,
    "Trade explorer": page_trade_explorer,
    "Range analysis": page_range_analysis,
    "Walk-forward": page_walkforward,
    "Predictive models": page_models,
}


def main() -> None:
    st.set_page_config(page_title="First Presented FVG research", layout="wide")
    st.sidebar.title("First Presented FVG")
    st.sidebar.caption("NQ / MNQ · historical research only — never places orders")
    page = st.sidebar.radio("Page", DASHBOARD_PAGES)
    config = _config()
    st.sidebar.markdown("---")
    st.sidebar.write(f"**Instrument:** {config.instrument}")
    st.sidebar.write(f"**Mode:** {config.contract_mode}" + (f" ({config.contract})" if config.contract else ""))
    st.sidebar.write(f"**Execution:** {config.execution.mode}")
    st.sidebar.write(f"**Inversion stop:** {config.inversion.stop_model}")
    run = _current_run()
    if run is not None:
        st.sidebar.markdown("---")
        st.sidebar.write(f"**Run:** {run.run_id}")
        st.sidebar.write(f"trades: {run.summary.get('trades', 0)}")
    PAGE_FUNCS[page]()


if __name__ == "__main__":  # pragma: no cover
    main()
