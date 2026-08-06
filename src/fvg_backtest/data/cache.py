"""Local Parquet cache for downloaded market data.

Layout::

    {cache_dir}/{source}/{root}/{resolution}/{symbol}_{YYYY-MM-DD}.parquet

One file per (symbol, resolution, UTC day) so partially-downloaded ranges
resume cheaply and the dashboard can show an inventory.  Cached frames are
already normalized, so reads skip provider parsing entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

UTC = timezone.utc


@dataclass
class CacheEntry:
    source: str
    root: str
    resolution: str
    symbol: str
    day: date
    path: Path
    rows: int
    bytes: int


class DataCache:
    def __init__(self, cache_dir: str | Path) -> None:
        self.root = Path(cache_dir)

    def _dir(self, source: str, root: str, resolution: str) -> Path:
        return self.root / source / root / resolution

    def path_for(self, source: str, root: str, resolution: str, symbol: str, day: date) -> Path:
        return self._dir(source, root, resolution) / f"{symbol}_{day.isoformat()}.parquet"

    # -- writing --------------------------------------------------------------

    def write(
        self, df: pl.DataFrame, *, source: str, root: str, resolution: str, symbol: str
    ) -> list[Path]:
        """Split a normalized frame by UTC day and persist it."""
        if df.is_empty():
            return []
        out: list[Path] = []
        with_day = df.with_columns(pl.col("timestamp_utc").dt.date().alias("_day"))
        for (day,), part in with_day.group_by(["_day"], maintain_order=True):
            p = self.path_for(source, root, resolution, symbol, day)
            p.parent.mkdir(parents=True, exist_ok=True)
            part.drop("_day").write_parquet(p)
            out.append(p)
        return out

    # -- reading --------------------------------------------------------------

    def missing_days(
        self, *, source: str, root: str, resolution: str, symbol: str,
        start: datetime, end: datetime,
    ) -> list[date]:
        days = []
        d = start.astimezone(UTC).date()
        last = (end.astimezone(UTC) - timedelta(microseconds=1)).date()
        while d <= last:
            if not self.path_for(source, root, resolution, symbol, d).exists():
                days.append(d)
            d += timedelta(days=1)
        return days

    def read(
        self, *, source: str, root: str, resolution: str, symbol: str,
        start: datetime, end: datetime,
    ) -> pl.DataFrame | None:
        paths = []
        d = start.astimezone(UTC).date()
        last = (end.astimezone(UTC) - timedelta(microseconds=1)).date()
        while d <= last:
            p = self.path_for(source, root, resolution, symbol, d)
            if p.exists():
                paths.append(p)
            d += timedelta(days=1)
        if not paths:
            return None
        df = pl.concat([pl.read_parquet(p) for p in paths], how="vertical_relaxed")
        return df.filter(
            (pl.col("timestamp_utc") >= start) & (pl.col("timestamp_utc") < end)
        ).sort("timestamp_utc")

    # -- inventory -------------------------------------------------------------

    def inventory(self) -> list[CacheEntry]:
        entries: list[CacheEntry] = []
        if not self.root.exists():
            return entries
        for p in sorted(self.root.rglob("*.parquet")):
            rel = p.relative_to(self.root).parts
            if len(rel) < 4:
                continue
            source, root, resolution = rel[0], rel[1], rel[2]
            stem = p.stem
            symbol, _, day_txt = stem.rpartition("_")
            try:
                day = date.fromisoformat(day_txt)
            except ValueError:
                continue
            try:
                rows = pl.scan_parquet(p).select(pl.len()).collect().item()
            except Exception:  # pragma: no cover - corrupt file
                rows = -1
            entries.append(
                CacheEntry(source, root, resolution, symbol, day, p, rows, p.stat().st_size)
            )
        return entries

    def inventory_frame(self) -> pl.DataFrame:
        entries = self.inventory()
        if not entries:
            return pl.DataFrame(
                schema={
                    "source": pl.String, "root": pl.String, "resolution": pl.String,
                    "symbol": pl.String, "day": pl.Date, "rows": pl.Int64,
                    "megabytes": pl.Float64, "path": pl.String,
                }
            )
        return pl.DataFrame(
            [
                {
                    "source": e.source, "root": e.root, "resolution": e.resolution,
                    "symbol": e.symbol, "day": e.day, "rows": e.rows,
                    "megabytes": round(e.bytes / 1e6, 3), "path": str(e.path),
                }
                for e in entries
            ]
        )
