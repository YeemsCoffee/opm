#!/usr/bin/env python
"""Regenerate sample_data/ from the deterministic scenario generator.

    python scripts/make_sample_data.py

Writes one-minute candles for NQ and MNQ over a month of sessions (both
dated contracts present so continuous-mode roll logic has something to
work with), plus a one-second file for one session so intrabar execution
modes can be exercised offline.
"""

from __future__ import annotations

from datetime import date, timezone
from pathlib import Path

import polars as pl

from fvg_backtest.config import AppConfig
from fvg_backtest.data.synthetic import SCENARIOS, generate_session_bars, explode_minute_to_seconds
from fvg_backtest.futures.contracts import contract_expiration, parse_contract
from fvg_backtest.sessions import SessionClock, TradingCalendar

UTC = timezone.utc
OUT = Path(__file__).resolve().parent.parent / "sample_data"

START = date(2025, 1, 2)
END = date(2025, 3, 31)
FRONT_UNTIL = date(2025, 3, 13)  # NQH25 leads volume until this session


def build(root: str, base_price: float) -> pl.DataFrame:
    cfg = AppConfig(instrument=root)
    clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    instrument = cfg.instruments[root]
    frames = []
    for i, d in enumerate(clock.calendar.trading_days(START, END)):
        scenario = SCENARIOS[i % len(SCENARIOS)]
        for code, offset in ((f"{root}H25", 0.0), (f"{root}M25", 95.0)):
            expiry = contract_expiration(parse_contract(code), instrument)
            if d > expiry:
                continue
            df = generate_session_bars(
                d, scenario, clock,
                base_price=base_price + offset,
                tick=instrument.tick_size,
                contract=code,
            )
            # the deferred contract trades thinner until it takes over
            lead = (code.endswith("H25") and d <= FRONT_UNTIL) or (
                code.endswith("M25") and d > FRONT_UNTIL
            )
            df = df.with_columns(
                (pl.col("volume") * (1.0 if lead else 0.12)).round(0).alias("volume")
            )
            frames.append(df)
    return pl.concat(frames).sort(["timestamp_utc", "underlying_contract"])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for root, base in (("NQ", 21000.0), ("MNQ", 21000.0)):
        df = build(root, base)
        path = OUT / f"{root}_1m_2025Q1.parquet"
        df.write_parquet(path)
        print(f"{path.name}: {df.height:,} rows, "
              f"{df['underlying_contract'].n_unique()} contracts")

        # a readable CSV slice of one session for eyeballing / CSV-provider
        # demo — in a subdirectory so `--path sample_data` does not read the
        # same session twice
        csv_dir = OUT / "csv"
        csv_dir.mkdir(exist_ok=True)
        one_day = df.filter(
            (pl.col("timestamp_utc") >= pl.datetime(2025, 1, 5, 23, 0, time_zone="UTC"))
            & (pl.col("timestamp_utc") < pl.datetime(2025, 1, 6, 22, 0, time_zone="UTC"))
            & (pl.col("underlying_contract") == f"{root}H25")
        )
        csv_path = csv_dir / f"{root}H25_1m_2025-01-06.csv"
        one_day.write_csv(csv_path)
        print(f"csv/{csv_path.name}: {one_day.height:,} rows")

    # one-second data for a single NQ session (intrabar execution demo)
    cfg = AppConfig(instrument="NQ")
    clock = SessionClock(config=cfg.sessions, calendar=TradingCalendar())
    minute = generate_session_bars(
        date(2025, 1, 6), "bullish_inversion", clock, base_price=21000.0, contract="NQH25"
    )
    ny = minute.with_columns(
        pl.col("timestamp_utc").dt.convert_time_zone("America/New_York").alias("ny")
    ).filter((pl.col("ny").dt.hour() >= 9) & (pl.col("ny").dt.hour() < 12)).drop("ny")
    seconds = [
        row
        for bar in ny.iter_rows(named=True)
        for row in explode_minute_to_seconds(bar, tick=0.25)
    ]
    sec_dir = OUT / "seconds"
    sec_dir.mkdir(exist_ok=True)
    sec_path = sec_dir / "NQH25_1s_2025-01-06.parquet"
    pl.DataFrame(seconds).write_parquet(sec_path)
    print(f"seconds/{sec_path.name}: {len(seconds):,} rows")


if __name__ == "__main__":
    main()
