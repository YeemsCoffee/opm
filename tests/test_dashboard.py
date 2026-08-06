"""Dashboard tests.

Streamlit page functions are exercised through ``streamlit.testing`` where
available; the data helpers and Plotly figures are always tested directly.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from fvg_backtest.config import AppConfig
from fvg_backtest.dashboard.state import (
    DASHBOARD_PAGES,
    categorical_columns,
    list_runs,
    load_run,
    numeric_columns,
    session_frames,
)
from fvg_backtest.data.synthetic import SyntheticProvider
from fvg_backtest.pipeline import run_backtest
from fvg_backtest.sessions import SessionClock, TradingCalendar
from fvg_backtest.visualization.charts import (
    conditional_bar_chart,
    drawdown_chart,
    equity_curve,
    monthly_bar_chart,
    range_probability_chart,
    trade_chart,
)

SESSION = date(2025, 1, 6)
LEAD_IN = [date(2025, 1, 2), date(2025, 1, 3), SESSION]


@pytest.fixture(scope="module")
def run_out():
    cfg = AppConfig(
        instrument="NQ", contract_mode="DATED", contract="NQH25",
        start=SESSION.isoformat(), end=SESSION.isoformat(),
    )
    clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    provider = SyntheticProvider(
        clock, cfg.instruments, schedule={d: "bullish_inversion" for d in LEAD_IN}
    )
    return run_backtest(cfg, provider=provider, keep_bars=True)


def test_all_pages_are_declared_and_wired():
    assert DASHBOARD_PAGES == [
        "Data", "Strategy settings", "Backtest", "Results",
        "Conditions explorer", "Trade explorer", "Range analysis",
        "Walk-forward", "Predictive models",
    ]
    from fvg_backtest.dashboard.app import PAGE_FUNCS

    assert set(PAGE_FUNCS) == set(DASHBOARD_PAGES)
    assert all(callable(f) for f in PAGE_FUNCS.values())


def test_run_store_roundtrip(tmp_path, run_out):
    base = run_out.write(tmp_path)
    assert run_out.run_id in list_runs(tmp_path)

    store = load_run(run_out.run_id, tmp_path)
    assert store.summary["instrument"] == "NQ"
    assert store.config is not None and store.config.contract == "NQH25"
    assert store.trades.height == run_out.trades.height
    assert store.sessions() == [SESSION]

    setup = store.session_setup(SESSION)
    assert setup is not None and setup["selected"] is True
    assert len(store.session_trades(SESSION)) == 2
    assert not store.session_events(SESSION).is_empty()
    assert store.instrument == "NQ"
    assert Path(base / "report.html").exists()


def test_load_run_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_run("nope", tmp_path)


def test_column_helpers(run_out):
    nums = numeric_columns(run_out.trades)
    cats = categorical_columns(run_out.trades)
    assert "result_r" in nums and "mae_r" in nums
    assert "order_kind" in cats and "direction" in cats
    assert "order_kind" not in nums


def test_session_frames_filters(run_out):
    bars = run_out.bars
    assert bars is not None
    one = session_frames(bars, SESSION)
    assert one["globex_session_date"].unique().to_list() == [SESSION]
    assert one["timestamp_utc"].is_sorted()


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------


def test_equity_and_drawdown_figures(run_out):
    fig = equity_curve(run_out.daily, in_dollars=True)
    names = [t.name for t in fig.data]
    assert "cumulative R" in names
    assert any("net" in n for n in names)
    assert drawdown_chart(run_out.daily).data


def test_figures_handle_empty_input():
    empty = pl.DataFrame()
    assert "no trades" in equity_curve(empty).layout.title.text
    assert "no trades" in drawdown_chart(empty).layout.title.text
    assert "no trades" in monthly_bar_chart(empty).layout.title.text


def test_conditional_chart_greys_out_small_samples():
    table = pl.DataFrame({
        "group": ["big", "small"],
        "trades": [40, 3],
        "expectancy_r": [0.2, 2.0],
        "reliable": [True, False],
    })
    fig = conditional_bar_chart(table, "expectancy_r")
    colors = list(fig.data[0].marker.color)
    assert colors[0] != colors[1]
    assert "small sample" in fig.data[0].text[1]


def test_range_probability_chart_is_a_percentage():
    table = pl.DataFrame({
        "group": ["a", "b"], "trades": [30, 30],
        "ranging_rate": [0.1, 0.6], "reliable": [True, True],
    })
    fig = range_probability_chart(table, "entry_delay_minutes")
    assert fig.layout.yaxis.tickformat == ".0%"
    assert "entry_delay_minutes" in fig.layout.title.text


def test_trade_chart_draws_the_whole_setup(run_out):
    bars = session_frames(run_out.bars, SESSION)
    cash = bars.filter(pl.col("session_segment") == "CASH")
    setup = run_out.setups.filter(pl.col("selected")).to_dicts()[0]
    trades = run_out.trades.to_dicts()
    events = run_out.events

    fig = trade_chart(
        cash, setup=setup, trades=trades, events=events,
        context=setup, pivots=[], title="test",
    )
    kinds = [type(t).__name__ for t in fig.data]
    assert "Candlestick" in kinds

    names = " ".join(str(t.name) for t in fig.data)
    assert "entry" in names
    assert "inversion" in names          # the inversion marker series
    assert "exit" in names

    # the zone rectangle and the Type B wick line are shapes, not traces
    assert any(s.type == "rect" for s in fig.layout.shapes)
    assert any(s.type == "line" for s in fig.layout.shapes)
    labels = " ".join(a.text or "" for a in fig.layout.annotations)
    assert "A_AND_B" in labels or "gap" in labels


def test_trade_chart_empty_bars():
    fig = trade_chart(pl.DataFrame())
    assert "No bars" in fig.layout.title.text


# --------------------------------------------------------------------------
# streamlit app smoke test
# --------------------------------------------------------------------------


def test_app_pages_render(tmp_path, run_out, monkeypatch):
    at = pytest.importorskip("streamlit.testing.v1", reason="streamlit testing API")
    run_out.write(tmp_path)

    app = at.AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
    app.session_state["config"] = run_out.config.model_copy(deep=True)
    app.session_state["config"].runs_dir = str(tmp_path)
    app.session_state["run"] = run_out
    app.session_state["bars"] = run_out.bars
    app.run(timeout=90)
    assert not app.exception

    for page in DASHBOARD_PAGES:
        app.sidebar.radio[0].set_value(page).run(timeout=90)
        assert not app.exception, f"{page} raised: {app.exception}"
        assert app.title or app.header, f"{page} rendered nothing"
