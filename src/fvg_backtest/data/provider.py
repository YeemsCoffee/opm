"""Market-data provider interface.

Contract for every provider:

- ``contract_mode="DATED"``: ``symbol`` is a dated contract code (``NQH25``)
  and the result contains that contract only.
- ``contract_mode="CONTINUOUS"``: ``symbol`` is a root (``NQ``/``MNQ``) and
  the result contains *all* dated contracts overlapping the range, one row
  per (contract, timestamp).  The continuous research series is then built
  explicitly by :mod:`fvg_backtest.futures.series` from a roll schedule —
  providers never splice contracts silently.

All results are normalized (:mod:`fvg_backtest.data.schema`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import polars as pl


@dataclass(frozen=True)
class ProviderRequest:
    symbol: str
    start: datetime  # tz-aware UTC, inclusive
    end: datetime  # tz-aware UTC, exclusive
    timeframe: str  # 1m | 1s | tick
    contract_mode: str  # DATED | CONTINUOUS


class MarketDataProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str,
        contract_mode: str,
    ) -> pl.DataFrame:
        """Return normalized candles for the request (see module docstring)."""

    def supports(self, timeframe: str) -> bool:
        return timeframe in ("1m", "1s", "tick")
