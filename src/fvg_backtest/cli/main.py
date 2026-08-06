"""``fvg-backtest`` command line interface.

    fvg-backtest contracts --root NQ
    fvg-backtest download --provider databento --symbol NQ \
        --start 2025-01-01 --end 2025-12-31 --resolution 1m
    fvg-backtest run --symbol NQ --config config/nq.yaml
    fvg-backtest compare --symbols NQ MNQ --start 2025-01-01 --end 2025-12-31
    fvg-backtest report --run-id <RUN_ID>
    fvg-backtest walkforward --symbol NQ --config config/nq.yaml
    fvg-backtest cache --list

This is a research tool: it reads historical data and writes files.  It
never connects to a broker and never places an order.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from ..analytics.stats import (
    compare_instruments,
    key_columns,
    normalized_comparison,
    summarize_trades,
)
from ..analytics.walkforward import run_walkforward
from ..config.loader import load_config
from ..config.schema import AppConfig
from ..data.cache import DataCache
from ..futures.contracts import contract_expiration, list_contracts
from ..pipeline import make_provider, run_backtest
from ..sessions.calendar import TradingCalendar
from ..sessions.clock import SessionClock

UTC = timezone.utc
DEFAULT_CONFIG = Path("config/default.yaml")


def _load(args, **overrides) -> AppConfig:
    paths = []
    if DEFAULT_CONFIG.exists():
        paths.append(DEFAULT_CONFIG)
    if getattr(args, "config", None):
        paths.append(args.config)
    over = {k: v for k, v in overrides.items() if v is not None}
    return load_config(*paths, overrides=over) if paths else AppConfig(**over)


def _clock(config: AppConfig) -> SessionClock:
    return SessionClock(config=config.sessions, calendar=TradingCalendar())


def _print(df: pl.DataFrame, limit: int = 40, *, narrow: bool = False) -> None:
    """Print a frame; ``narrow`` keeps only the headline stats columns so the
    table stays readable in a terminal (the full block is still on disk)."""
    if df.is_empty():
        print("(no rows)")
        return
    if narrow:
        df = key_columns(df)
    with pl.Config(tbl_rows=limit, tbl_cols=-1, tbl_width_chars=200):
        print(df)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_contracts(args) -> int:
    config = _load(args)
    root = args.root.upper()
    if root not in config.instruments:
        print(f"unknown root {root!r}; configured: {sorted(config.instruments)}", file=sys.stderr)
        return 2
    instrument = config.instruments[root]
    start = date.fromisoformat(args.start) if args.start else date.today() - timedelta(days=365)
    end = date.fromisoformat(args.end) if args.end else date.today() + timedelta(days=365)
    rows = [
        {
            "contract": c.code,
            "databento_symbol": c.databento_raw,
            "expiration": contract_expiration(c, instrument),
            "tick_size": instrument.tick_size,
            "point_value": instrument.point_value,
            "tick_value": instrument.tick_value,
            "exchange": instrument.exchange,
            "dataset": instrument.databento.dataset,
        }
        for c in list_contracts(instrument, start, end)
    ]
    _print(pl.DataFrame(rows))
    return 0


def cmd_download(args) -> int:
    config = _load(
        args,
        instrument=args.symbol.upper() if len(args.symbol) <= 3 else None,
        start=args.start,
        end=args.end,
    )
    config.data.provider = args.provider
    if args.path:
        config.data.path = args.path
    clock = _clock(config)
    provider = make_provider(config, clock)

    start = clock.ny_datetime(date.fromisoformat(args.start), "18:00").astimezone(UTC)
    end = clock.ny_datetime(date.fromisoformat(args.end) + timedelta(days=1), "17:00").astimezone(UTC)
    mode = "DATED" if len(args.symbol) > 3 else "CONTINUOUS"
    print(
        f"downloading {args.symbol} {args.resolution} "
        f"[{args.start} .. {args.end}] via {args.provider} ({mode})…"
    )
    bars = provider.get_bars(args.symbol.upper(), start, end, args.resolution, mode)
    print(f"  {bars.height:,} rows")
    if "underlying_contract" in bars.columns:
        counts = (
            bars.group_by("underlying_contract")
            .agg(pl.len().alias("rows"))
            .sort("underlying_contract")
        )
        _print(counts)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        bars.write_parquet(args.out)
        print(f"  written to {args.out}")
    return 0


def cmd_run(args) -> int:
    config = _load(
        args,
        instrument=args.symbol.upper() if args.symbol else None,
        start=args.start,
        end=args.end,
        contract=args.contract,
        contract_mode="DATED" if args.contract else None,
    )
    if args.provider:
        config.data.provider = args.provider
    if args.path:
        config.data.path = args.path
    if args.execution_mode:
        config.execution.mode = args.execution_mode

    out = run_backtest(config, run_id=args.run_id)
    print(json.dumps(
        {k: v for k, v in out.summary.items() if k not in ("data_quality", "event_counts")},
        indent=2, default=str,
    ))
    if out.quality and (out.quality.errors or out.quality.warnings):
        print("\ndata quality:")
        for issue in out.quality.errors + out.quality.warnings:
            print(f"  [{issue.severity}] {issue.check}: {issue.message}")
    base = out.write(args.runs_dir)
    print(f"\nrun written to {base}")
    if not out.trades.is_empty():
        print()
        _print(
            pl.DataFrame([summarize_trades(out.trades, group=config.instrument)]),
            narrow=True,
        )
    return 0


def cmd_compare(args) -> int:
    frames: dict[str, pl.DataFrame] = {}
    for symbol in args.symbols:
        config = _load(args, instrument=symbol.upper(), start=args.start, end=args.end)
        if args.provider:
            config.data.provider = args.provider
        if args.path:
            config.data.path = args.path
        out = run_backtest(config)
        frames[symbol.upper()] = out.trades
        print(f"{symbol.upper()}: {out.trades.height} trades, "
              f"{out.summary.get('qualifying_fvgs', 0)} qualifying FVGs")
        if args.runs_dir:
            out.write(args.runs_dir)

    print("\n=== per instrument ===")
    _print(compare_instruments(frames), narrow=True)
    print("\n=== same sessions only ===")
    _print(normalized_comparison(frames), narrow=True)
    return 0


def cmd_report(args) -> int:
    base = Path(args.runs_dir or "runs") / args.run_id
    if not base.exists():
        print(f"no such run: {base}", file=sys.stderr)
        return 2
    summary = json.loads((base / "summary.json").read_text())
    print(json.dumps(summary, indent=2, default=str))
    trades_path = base / "trades.parquet"
    if trades_path.exists():
        trades = pl.read_parquet(trades_path)
        if not trades.is_empty():
            print("\n=== overall ===")
            _print(pl.DataFrame([summarize_trades(trades)]), narrow=True)
            if args.by:
                from ..analytics.stats import conditional_table

                print(f"\n=== by {args.by} ===")
                _print(
                    conditional_table(trades, args.by, bins=args.bins), narrow=True
                )
    print(f"\nHTML report: {base / 'report.html'}")
    return 0


def cmd_walkforward(args) -> int:
    config = _load(args, instrument=args.symbol.upper() if args.symbol else None,
                   start=args.start, end=args.end)
    if args.provider:
        config.data.provider = args.provider
    if args.path:
        config.data.path = args.path

    clock = _clock(config)
    provider = make_provider(config, clock)

    def run_fn(cfg: AppConfig, a: date, b: date) -> pl.DataFrame:
        scoped = cfg.model_copy(deep=True)
        scoped.start, scoped.end = a.isoformat(), b.isoformat()
        try:
            return run_backtest(scoped, provider=provider).trades
        except ValueError:
            return pl.DataFrame()

    result = run_walkforward(config, run_fn)
    print("=== folds ===")
    fold_cols = [
        c for c in (
            "fold", "dev_start", "oos_end", "dev_trades", "dev_expectancy_r",
            "val_trades", "val_expectancy_r", "oos_trades", "oos_expectancy_r",
            "degradation_r", "holds_out_of_sample",
        ) if c in result.folds.columns
    ]
    _print(result.folds.select(fold_cols) if fold_cols else result.folds)
    print("\n=== chosen parameters ===")
    _print(result.parameters)
    for w in result.warnings:
        print(f"  ! {w}")
    if not result.folds.is_empty() and args.runs_dir:
        base = Path(args.runs_dir) / (args.run_id or "walkforward")
        base.mkdir(parents=True, exist_ok=True)
        result.folds.write_parquet(base / "walkforward_folds.parquet")
        print(f"\nwritten to {base}")
    return 0


def cmd_cache(args) -> int:
    config = _load(args)
    cache = DataCache(args.cache_dir or config.data.cache_dir)
    inv = cache.inventory_frame()
    if inv.is_empty():
        print(f"cache {cache.root} is empty")
        return 0
    print(f"cache: {cache.root}")
    _print(
        inv.group_by(["source", "root", "resolution", "symbol"])
        .agg(
            pl.len().alias("days"),
            pl.col("rows").sum().alias("rows"),
            pl.col("megabytes").sum().round(2).alias("megabytes"),
            pl.col("day").min().alias("from"),
            pl.col("day").max().alias("to"),
        )
        .sort(["root", "symbol", "resolution"])
    )
    return 0


def cmd_credentials(args) -> int:
    from ..data.databento_provider import credential_status

    status = credential_status()
    print(json.dumps(status, indent=2))
    return 0 if status["present"] else 1


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fvg-backtest",
        description=(
            "First Presented FVG research for NQ / MNQ futures. "
            "Historical backtesting only — never places live orders."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--config", help="YAML overlay on config/default.yaml")
        sp.add_argument("--provider", choices=["databento", "csv", "parquet", "synthetic"])
        sp.add_argument("--path", help="file/directory for the csv or parquet provider")
        sp.add_argument("--runs-dir", default="runs")
        return sp

    c = sub.add_parser("contracts", help="list dated contracts and instrument metadata")
    c.add_argument("--root", default="NQ")
    c.add_argument("--start")
    c.add_argument("--end")
    c.add_argument("--config")
    c.set_defaults(func=cmd_contracts)

    d = common(sub.add_parser("download", help="fetch and cache market data"))
    d.add_argument("--symbol", required=True, help="NQ, MNQ, or a dated contract like NQH25")
    d.add_argument("--start", required=True)
    d.add_argument("--end", required=True)
    d.add_argument("--resolution", default="1m", choices=["1m", "1s", "tick"])
    d.add_argument("--out", help="also write the normalized frame to this parquet file")
    d.set_defaults(func=cmd_download, provider="databento")

    r = common(sub.add_parser("run", help="run the backtest and write a run directory"))
    r.add_argument("--symbol", help="NQ or MNQ")
    r.add_argument("--contract", help="dated contract, e.g. NQH25 (implies DATED mode)")
    r.add_argument("--start")
    r.add_argument("--end")
    r.add_argument("--run-id")
    r.add_argument(
        "--execution-mode",
        choices=["ONE_MINUTE_CONSERVATIVE", "ONE_SECOND_INTRABAR", "TICK_INTRABAR"],
    )
    r.set_defaults(func=cmd_run)

    cp = common(sub.add_parser("compare", help="compare instruments on the same settings"))
    cp.add_argument("--symbols", nargs="+", default=["NQ", "MNQ"])
    cp.add_argument("--start")
    cp.add_argument("--end")
    cp.set_defaults(func=cmd_compare)

    rp = sub.add_parser("report", help="print a stored run's summary")
    rp.add_argument("--run-id", required=True)
    rp.add_argument("--runs-dir", default="runs")
    rp.add_argument("--by", help="group the trades by this column")
    rp.add_argument("--bins", type=int, help="quantile bins for a numeric --by column")
    rp.add_argument("--config")
    rp.set_defaults(func=cmd_report)

    wf = common(sub.add_parser("walkforward", help="chronological walk-forward validation"))
    wf.add_argument("--symbol")
    wf.add_argument("--start")
    wf.add_argument("--end")
    wf.add_argument("--run-id")
    wf.set_defaults(func=cmd_walkforward)

    ch = sub.add_parser("cache", help="show the local data cache inventory")
    ch.add_argument("--cache-dir")
    ch.add_argument("--config")
    ch.set_defaults(func=cmd_cache)

    cr = sub.add_parser("credentials", help="check the Databento credential (never printed)")
    cr.add_argument("--config")
    cr.set_defaults(func=cmd_credentials)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
