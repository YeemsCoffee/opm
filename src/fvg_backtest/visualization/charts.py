"""Plotly figures for the dashboard.

The trade chart is the centrepiece: one session's candles annotated with the
selected zone, its Type A/B evidence, the qualifying prior wick, entry, stop,
target, eligible pivots, context levels, and every mitigation / inversion
event — enough to check a trade by eye against the numbers.
"""

from __future__ import annotations

import plotly.graph_objects as go
import polars as pl

BULL = "#1a9850"
BEAR = "#d73027"
ZONE_BULL = "rgba(26, 152, 80, 0.18)"
ZONE_BEAR = "rgba(215, 48, 39, 0.18)"
GRID = "rgba(128,128,128,0.2)"


def _layout(fig: go.Figure, title: str, height: int = 640) -> go.Figure:
    fig.update_layout(
        title=title,
        height=height,
        margin=dict(l=50, r=30, t=60, b=40),
        hovermode="x unified",
        template="plotly_white",
        xaxis=dict(gridcolor=GRID),
        yaxis=dict(gridcolor=GRID),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
    )
    return fig


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


def equity_curve(daily: pl.DataFrame, *, in_dollars: bool = False) -> go.Figure:
    fig = go.Figure()
    if daily.is_empty():
        return _layout(fig, "Equity curve — no trades", 320)
    x = daily["session_date"].to_list()
    fig.add_trace(go.Scatter(x=x, y=daily["cumulative_r"], name="cumulative R", line=dict(width=2)))
    if "cumulative_net_r" in daily.columns:
        fig.add_trace(
            go.Scatter(
                x=x, y=daily["cumulative_net_r"], name="cumulative R (net of costs)",
                line=dict(width=2, dash="dot"),
            )
        )
    if in_dollars and "cumulative_net_dollars" in daily.columns:
        fig.add_trace(
            go.Scatter(
                x=x, y=daily["cumulative_net_dollars"], name="cumulative net $",
                yaxis="y2", line=dict(width=1.5, color="#555"),
            )
        )
        fig.update_layout(yaxis2=dict(overlaying="y", side="right", title="net $"))
    fig.add_hline(y=0, line_dash="dash", line_color="grey")
    return _layout(fig, "Equity curve", 420)


def drawdown_chart(daily: pl.DataFrame) -> go.Figure:
    fig = go.Figure()
    if daily.is_empty() or "drawdown_r" not in daily.columns:
        return _layout(fig, "Drawdown — no trades", 260)
    fig.add_trace(
        go.Scatter(
            x=daily["session_date"].to_list(), y=daily["drawdown_r"],
            fill="tozeroy", name="drawdown (R)", line=dict(color=BEAR, width=1),
        )
    )
    return _layout(fig, "Drawdown in R", 260)


def monthly_bar_chart(trades: pl.DataFrame) -> go.Figure:
    fig = go.Figure()
    if trades.is_empty():
        return _layout(fig, "Monthly results — no trades", 320)
    monthly = (
        trades.with_columns(pl.col("session_date").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(
            pl.col("result_r").sum().alias("total_r"),
            pl.col("net_result_r").sum().alias("net_total_r"),
            pl.len().alias("trades"),
        )
        .sort("month")
    )
    fig.add_trace(go.Bar(x=monthly["month"], y=monthly["total_r"], name="gross R"))
    fig.add_trace(go.Bar(x=monthly["month"], y=monthly["net_total_r"], name="net R"))
    fig.update_layout(barmode="group")
    return _layout(fig, "Monthly results", 340)


def conditional_bar_chart(
    table: pl.DataFrame, metric: str = "expectancy_r", label: str | None = None
) -> go.Figure:
    fig = go.Figure()
    if table.is_empty() or metric not in table.columns:
        return _layout(fig, "No data", 320)
    reliable = table["reliable"].to_list() if "reliable" in table.columns else [True] * table.height
    colors = ["#4575b4" if r else "#bbbbbb" for r in reliable]
    text = [
        f"n={n}" + ("" if r else " (small sample)")
        for n, r in zip(table["trades"].to_list(), reliable)
    ]
    fig.add_trace(
        go.Bar(
            x=table["group"].to_list(), y=table[metric].to_list(),
            marker_color=colors, text=text, textposition="outside",
            name=metric,
        )
    )
    fig.add_hline(y=0, line_dash="dash", line_color="grey")
    return _layout(fig, label or f"{metric} by group", 420)


def range_probability_chart(table: pl.DataFrame, by: str) -> go.Figure:
    fig = conditional_bar_chart(table, "ranging_rate", f"Probability of ranging by {by}")
    fig.update_yaxes(tickformat=".0%", range=[0, 1])
    return fig


# ---------------------------------------------------------------------------
# trade explorer
# ---------------------------------------------------------------------------


def trade_chart(
    bars: pl.DataFrame,
    setup: dict | None = None,
    trades: list[dict] | None = None,
    events: pl.DataFrame | None = None,
    context: dict | None = None,
    pivots: list[dict] | None = None,
    *,
    title: str = "",
) -> go.Figure:
    """Annotated candlestick chart for one session."""
    fig = go.Figure()
    if bars.is_empty():
        return _layout(fig, "No bars", 420)

    x = bars["timestamp_ny"].to_list()
    fig.add_trace(
        go.Candlestick(
            x=x,
            open=bars["open"], high=bars["high"], low=bars["low"], close=bars["close"],
            name="1-minute", increasing_line_color=BULL, decreasing_line_color=BEAR,
            showlegend=False,
        )
    )
    x0, x1 = x[0], x[-1]

    if setup:
        bullish = setup.get("direction") == "BULLISH"
        fig.add_shape(
            type="rect", x0=setup["c3_time"], x1=x1,
            y0=setup["fvg_low"], y1=setup["fvg_high"],
            fillcolor=ZONE_BULL if bullish else ZONE_BEAR,
            line=dict(width=1, color=BULL if bullish else BEAR), layer="below",
        )
        fig.add_hline(
            y=setup["midpoint"], line=dict(dash="dot", width=1, color="#777"),
            annotation_text="zone midpoint", annotation_position="right",
        )
        label = (
            f"{setup.get('significance_type') or 'candidate'} · "
            f"gap {setup['gap_width']:.2f}pt "
            f"({setup.get('type_a_normalized_gap', 0):.2f} ATR) · "
            f"preservation {setup.get('type_a_preservation_ratio', 0):.2f}"
        )
        fig.add_annotation(
            x=setup["c3_time"], y=setup["fvg_high"], text=label, showarrow=True,
            arrowhead=2, ax=40, ay=-40, bgcolor="rgba(255,255,255,0.85)",
        )
        # the prior wick that satisfied Type B
        wick_time = setup.get("type_b_closest_wick_timestamp")
        if wick_time is not None:
            fig.add_vline(
                x=wick_time, line=dict(color="#7b3294", width=1.5, dash="dash"),
                annotation_text="Type B wick", annotation_position="top",
            )

    for level, name, color in (
        ("open_price", "09:30 open", "#333"),
        ("ctx_overnight_high", "overnight high", "#888"),
        ("ctx_overnight_low", "overnight low", "#888"),
        ("ctx_opening_range_long_high", "opening range high", "#3182bd"),
        ("ctx_opening_range_long_low", "opening range low", "#3182bd"),
    ):
        value = (context or {}).get(level)
        if value is not None:
            fig.add_hline(
                y=value, line=dict(color=color, width=1, dash="dot"),
                annotation_text=name, annotation_position="left",
                annotation_font_size=10,
            )

    for pivot in pivots or []:
        fig.add_trace(
            go.Scatter(
                x=[pivot["timestamp"]], y=[pivot["price"]], mode="markers",
                marker=dict(
                    symbol="triangle-down" if pivot["side"] == "HIGH" else "triangle-up",
                    size=9,
                    color="#999" if pivot.get("status") == "SWEPT" else "#e6550d",
                ),
                name=f"{pivot['side']} {pivot.get('status', '')}",
                showlegend=False,
                hovertext=f"{pivot['side']} {pivot['price']:.2f} ({pivot.get('status')})",
            )
        )

    for i, trade in enumerate(trades or []):
        entry_t = trade.get("entry_time") or trade.get("filled_at")
        exit_t = trade.get("exit_time") or x1
        for key, name, color, dash in (
            ("entry_price", "entry", "#2166ac", "solid"),
            ("stop_price", "stop", BEAR, "dash"),
            ("target_price", "target", BULL, "dash"),
        ):
            value = trade.get(key)
            if value is None:
                continue
            fig.add_shape(
                type="line", x0=entry_t or x0, x1=exit_t, y0=value, y1=value,
                line=dict(color=color, width=2, dash=dash),
            )
            fig.add_annotation(
                x=exit_t, y=value, text=f"{name} {value:.2f}", showarrow=False,
                xanchor="left", font=dict(size=10, color=color),
            )
        if entry_t is not None:
            fig.add_trace(
                go.Scatter(
                    x=[entry_t], y=[trade.get("entry_fill", trade.get("entry_price"))],
                    mode="markers", marker=dict(symbol="circle", size=11, color="#2166ac"),
                    name=f"{trade.get('order_kind', 'trade')} entry",
                    hovertext=f"{trade.get('order_kind')} {trade.get('direction')}",
                )
            )
        if trade.get("exit_time") is not None:
            won = (trade.get("result_r") or 0) > 0
            fig.add_trace(
                go.Scatter(
                    x=[trade["exit_time"]], y=[trade.get("exit_fill")],
                    mode="markers",
                    marker=dict(symbol="x", size=11, color=BULL if won else BEAR),
                    name=f"exit {trade.get('exit_reason')} ({trade.get('result_r'):.2f}R)",
                )
            )

    if events is not None and not events.is_empty():
        marks = {
            "INVERSION": ("#762a83", "star", 13),
            "REINVERSION": ("#9970ab", "star", 11),
            "MITIGATION": ("#bbbbbb", "line-ns", 7),
            "RANGE_ONSET": ("#e08214", "diamond", 12),
        }
        for kind, (color, symbol, size) in marks.items():
            part = events.filter(pl.col("event") == kind)
            if part.is_empty():
                continue
            ys = part["price"].to_list() if "price" in part.columns else None
            fig.add_trace(
                go.Scatter(
                    x=part["timestamp"].to_list(), y=ys, mode="markers",
                    marker=dict(color=color, symbol=symbol, size=size),
                    name=kind.lower(), showlegend=kind != "MITIGATION",
                )
            )

    fig.update_layout(xaxis_rangeslider_visible=False)
    return _layout(fig, title or "Session", 700)
