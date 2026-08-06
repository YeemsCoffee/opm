"""CSV / Parquet offline providers.

Accepts a single file or a directory (all ``*.csv`` / ``*.parquet`` inside).
Column names are matched case-insensitively with common aliases, so raw
Databento CSV exports, TradingView exports, and our own sample data all load
without editing.  Timestamps may be ISO strings, epoch seconds/millis, or
datetimes; tz-naive values are assumed UTC.

Required: timestamp + OHLC.  ``underlying_contract`` should be a column;
for single-contract files it may instead be inferred from the file name
(e.g. ``NQH25_1m.csv``) or passed as the requested symbol in DATED mode.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import polars as pl

from ..config.schema import InstrumentConfig
from ..futures.contracts import parse_contract
from ..sessions.clock import SessionClock
from .provider import MarketDataProvider
from .schema import normalize_candles

_ALIASES = {
    "timestamp_utc": ["timestamp_utc", "timestamp", "ts", "ts_event", "time", "datetime", "date_time"],
    "open": ["open", "o"],
    "high": ["high", "h"],
    "low": ["low", "l"],
    "close": ["close", "c"],
    "volume": ["volume", "vol", "size", "v"],
    "trade_count": ["trade_count", "trades", "n_trades", "count"],
    "vwap": ["vwap"],
    "underlying_contract": ["underlying_contract", "contract", "raw_symbol", "instrument", "symbol_dated"],
    "symbol": ["symbol", "ticker"],
}

_CONTRACT_IN_NAME = re.compile(r"((?:M?NQ)[FGHJKMNQUVXZ]\d{1,2})", re.IGNORECASE)


def _std_columns(df: pl.DataFrame) -> pl.DataFrame:
    lower = {c.lower(): c for c in df.columns}
    rename: dict[str, str] = {}
    for canon, names in _ALIASES.items():
        if canon in df.columns:
            continue
        for n in names:
            if n in lower and lower[n] not in rename:
                rename[lower[n]] = canon
                break
    df = df.rename(rename)
    return df


def _coerce_timestamp(df: pl.DataFrame) -> pl.DataFrame:
    dtype = df.schema.get("timestamp_utc")
    if dtype in (pl.Int64, pl.UInt64, pl.Int32, pl.Float64):
        # epoch: decide seconds vs millis vs nanos by magnitude
        mx = df["timestamp_utc"].max()
        unit = "s"
        if mx and mx > 10**17:
            unit = "ns"
        elif mx and mx > 10**14:
            unit = "us"
        elif mx and mx > 10**11:
            unit = "ms"
        df = df.with_columns(
            pl.from_epoch(pl.col("timestamp_utc").cast(pl.Int64), time_unit=unit)
            .dt.replace_time_zone("UTC")
            .alias("timestamp_utc")
        )
    elif dtype == pl.String:
        df = df.with_columns(
            pl.col("timestamp_utc").str.to_datetime(time_zone="UTC", time_unit="us")
        )
    return df


class FileProvider(MarketDataProvider):
    name = "csv"

    def __init__(
        self,
        path: str | Path,
        clock: SessionClock,
        instruments: dict[str, InstrumentConfig],
        source_label: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self.instruments = instruments
        if source_label:
            self.name = source_label

    # -- reading -------------------------------------------------------------

    def _files(self) -> list[Path]:
        if self.path.is_dir():
            # top level only — subdirectories are left to an explicit path so
            # a directory of exports cannot silently pull in extra copies
            files = sorted([*self.path.glob("*.csv"), *self.path.glob("*.parquet")])
            if not files:
                raise FileNotFoundError(f"no .csv/.parquet files in {self.path}")
            return files
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        return [self.path]

    def _read_one(self, f: Path) -> pl.DataFrame:
        if f.suffix == ".parquet":
            df = pl.read_parquet(f)
        else:
            df = pl.read_csv(f, try_parse_dates=True, infer_schema_length=2000)
        df = _std_columns(df)
        df = _coerce_timestamp(df)
        if "underlying_contract" not in df.columns:
            m = _CONTRACT_IN_NAME.search(f.stem)
            if m:
                df = df.with_columns(
                    pl.lit(m.group(1).upper()).alias("underlying_contract")
                )
        return df

    def _load(self) -> pl.DataFrame:
        frames = [self._read_one(f) for f in self._files()]
        cols = [
            "timestamp_utc", "open", "high", "low", "close",
            "volume", "trade_count", "vwap", "underlying_contract",
        ]
        out = []
        for fr in frames:
            missing = [c for c in ("timestamp_utc", "open", "high", "low", "close") if c not in fr.columns]
            if missing:
                raise ValueError(f"file missing required columns {missing}")
            for c in cols:
                if c not in fr.columns:
                    fr = fr.with_columns(pl.lit(None).alias(c))
            out.append(fr.select(cols))
        combined = pl.concat(out, how="vertical_relaxed")
        # overlapping files are common (a monthly export plus a daily one);
        # identical rows are harmless, so drop them rather than failing.
        # Rows that disagree on price survive and are reported by the
        # data-quality check as duplicate timestamps.
        return combined.unique(keep="first", maintain_order=True)

    # -- provider API ----------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str,
        contract_mode: str,
    ) -> pl.DataFrame:
        if timeframe not in ("1m", "1s"):
            raise ValueError(f"{self.name} provider serves 1m/1s candles, not {timeframe!r}")
        raw = self._load()

        if contract_mode == "DATED":
            contract = parse_contract(symbol)
            root = contract.root
            if raw["underlying_contract"].null_count() == raw.height:
                # single-contract file with no contract info: trust the request
                raw = raw.with_columns(pl.lit(contract.code).alias("underlying_contract"))
            raw = raw.filter(pl.col("underlying_contract").str.to_uppercase() == contract.code)
        else:
            root = symbol.upper()
            if raw["underlying_contract"].null_count() > 0:
                raise ValueError(
                    "CONTINUOUS mode needs an underlying_contract column (or "
                    "contract codes in file names) — contracts are never guessed"
                )
            raw = raw.filter(
                pl.col("underlying_contract").str.to_uppercase().str.starts_with(root)
                # exclude e.g. MNQ rows when NQ requested
                & ~pl.col("underlying_contract")
                .str.to_uppercase()
                .str.starts_with("M" + root)
            )
        if root not in self.instruments:
            raise ValueError(f"unknown root symbol {root!r}")

        raw = raw.filter(
            (pl.col("timestamp_utc").dt.replace_time_zone("UTC") >= start)
            & (pl.col("timestamp_utc").dt.replace_time_zone("UTC") < end)
            if raw.schema["timestamp_utc"].time_zone is None  # type: ignore[union-attr]
            else (pl.col("timestamp_utc") >= start) & (pl.col("timestamp_utc") < end)
        )
        if raw.is_empty():
            raise ValueError(
                f"no rows for {symbol} in [{start:%Y-%m-%d} .. {end:%Y-%m-%d}) — "
                f"check the file(s) under {self.path}"
            )
        return normalize_candles(
            raw,
            clock=self.clock,
            symbol=symbol.upper(),
            root_symbol=root,
            source=self.name,
            resolution=timeframe,
        )


class ParquetProvider(FileProvider):
    name = "parquet"
