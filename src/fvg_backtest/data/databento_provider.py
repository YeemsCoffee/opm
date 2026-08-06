"""Databento historical market-data provider.

Reads ``DATABENTO_API_KEY`` from the environment (``.env`` supported).
Handles symbol mapping (parent / dated / continuous), request chunking
("pagination" by day-range), rate-limit retry with exponential backoff, and
local Parquet caching.

Symbology
---------
=================  ======================  ==============================
request            stype_in                example
=================  ======================  ==============================
dated contract     ``raw_symbol``          ``NQH5`` (from ``NQH25``)
all contracts      ``parent``              ``NQ.FUT``
lead continuous    ``continuous``          ``NQ.c.0``
=================  ======================  ==============================

CONTINUOUS mode requests the **parent** symbol so every dated contract comes
back separately with its own ``underlying_contract``; the research series is
then assembled explicitly from a roll schedule.  Databento's own ``.c.0``
continuous symbol is available via ``lead_continuous=True`` but is *not* the
default, because the roll rule must stay explicit and inspectable.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import polars as pl

from ..config.schema import DataConfig, InstrumentConfig
from ..futures.contracts import parse_contract
from ..sessions.clock import SessionClock
from .cache import DataCache
from .provider import MarketDataProvider
from .schema import normalize_candles

UTC = timezone.utc

_SCHEMA_FOR = {"1m": "ohlcv-1m", "1s": "ohlcv-1s", "tick": "trades"}
_CHUNK_DAYS = {"1m": 30, "1s": 2, "tick": 1}


class DatabentoCredentialError(RuntimeError):
    pass


def credential_status(api_key: str | None = None) -> dict:
    """Report credential presence without ever echoing the key."""
    key = api_key or os.environ.get("DATABENTO_API_KEY", "")
    key = key.strip()
    if not key:
        return {"present": False, "valid_format": False, "hint": "DATABENTO_API_KEY not set"}
    ok = key.startswith("db-") and len(key) > 20
    return {
        "present": True,
        "valid_format": ok,
        "masked": f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…",
        "hint": "" if ok else "keys normally start with 'db-'",
    }


class DatabentoProvider(MarketDataProvider):
    name = "databento"

    def __init__(
        self,
        clock: SessionClock,
        instruments: dict[str, InstrumentConfig],
        data_config: DataConfig | None = None,
        api_key: str | None = None,
        client=None,
        max_retries: int = 5,
        sleeper=time.sleep,
    ) -> None:
        self.clock = clock
        self.instruments = instruments
        self.data_config = data_config or DataConfig()
        self.cache = DataCache(self.data_config.cache_dir)
        self._api_key = api_key or os.environ.get("DATABENTO_API_KEY")
        self._client = client
        self.max_retries = max_retries
        self._sleep = sleeper

    # -- client ---------------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            if not self._api_key:
                raise DatabentoCredentialError(
                    "DATABENTO_API_KEY is not set — put it in .env or the environment "
                    "(see .env.example), or use the csv/parquet provider offline"
                )
            import databento as db  # imported lazily so offline use needs no key

            self._client = db.Historical(key=self._api_key)
        return self._client

    # -- symbology --------------------------------------------------------------

    def resolve_symbology(
        self, symbol: str, contract_mode: str, *, lead_continuous: bool = False
    ) -> tuple[str, str, str, str]:
        """Return (root, provider_symbol, stype_in, dataset)."""
        if contract_mode == "DATED":
            contract = parse_contract(symbol)
            inst = self._instrument(contract.root)
            return contract.root, contract.databento_raw, "raw_symbol", inst.databento.dataset
        root = symbol.upper()
        inst = self._instrument(root)
        if lead_continuous:
            return root, inst.databento.continuous_symbol, "continuous", inst.databento.dataset
        return root, inst.databento.parent_symbol, "parent", inst.databento.dataset

    def _instrument(self, root: str) -> InstrumentConfig:
        if root not in self.instruments:
            raise ValueError(f"unknown root symbol {root!r}; configured: {sorted(self.instruments)}")
        return self.instruments[root]

    # -- metadata ---------------------------------------------------------------

    def list_contracts(self, root: str, start: datetime, end: datetime) -> list[str]:
        """Dated contracts that actually traded in the window, per the provider."""
        inst = self._instrument(root)
        res = self._with_retry(
            lambda: self.client.symbology.resolve(
                dataset=inst.databento.dataset,
                symbols=[inst.databento.parent_symbol],
                stype_in="parent",
                stype_out="raw_symbol",
                start_date=start.astimezone(UTC).date().isoformat(),
                end_date=end.astimezone(UTC).date().isoformat(),
            )
        )
        mappings = res.get("result", res) if isinstance(res, dict) else {}
        out: set[str] = set()
        for entries in mappings.values():
            for e in entries:
                sym = e.get("s") if isinstance(e, dict) else None
                if sym:
                    out.add(sym)
        return sorted(out)

    # -- retry / rate limits -------------------------------------------------------

    def _with_retry(self, call):
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - provider raises varied types
                msg = str(exc).lower()
                retryable = any(
                    s in msg
                    for s in ("rate limit", "429", "too many requests", "timeout",
                              "503", "502", "connection", "temporarily")
                )
                if not retryable or attempt == self.max_retries - 1:
                    raise
                last = exc
                self._sleep(delay)
                delay *= 2
        raise last  # pragma: no cover

    # -- fetching ------------------------------------------------------------------

    def _chunks(self, start: datetime, end: datetime, timeframe: str):
        step = timedelta(days=_CHUNK_DAYS.get(timeframe, 7))
        cursor = start
        while cursor < end:
            stop = min(cursor + step, end)
            yield cursor, stop
            cursor = stop

    def _fetch_chunk(
        self, dataset: str, provider_symbol: str, stype_in: str,
        start: datetime, end: datetime, timeframe: str,
    ) -> pl.DataFrame:
        schema = _SCHEMA_FOR[timeframe]

        def call():
            return self.client.timeseries.get_range(
                dataset=dataset,
                symbols=[provider_symbol],
                stype_in=stype_in,
                schema=schema,
                start=start,
                end=end,
            )

        store = self._with_retry(call)
        pdf = store.to_df()
        if pdf is None or len(pdf) == 0:
            return pl.DataFrame()
        pdf = pdf.reset_index()
        return pl.from_pandas(pdf)

    @staticmethod
    def _standardize(df: pl.DataFrame, timeframe: str) -> pl.DataFrame:
        """Map Databento columns onto the normalizer's expected names."""
        rename = {}
        for src, dst in (
            ("ts_event", "timestamp_utc"),
            ("index", "timestamp_utc"),
            ("symbol", "underlying_contract"),
            ("size", "volume"),
        ):
            if src in df.columns and dst not in df.columns:
                rename[src] = dst
        df = df.rename(rename)
        if timeframe == "tick":
            keep = [c for c in ("timestamp_utc", "price", "size", "underlying_contract") if c in df.columns]
            return df.select(keep)
        keep = [
            c for c in ("timestamp_utc", "open", "high", "low", "close", "volume",
                        "trade_count", "vwap", "underlying_contract")
            if c in df.columns
        ]
        return df.select(keep)

    # -- provider API -----------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str,
        contract_mode: str,
        *,
        use_cache: bool = True,
        lead_continuous: bool = False,
    ) -> pl.DataFrame:
        if timeframe not in _SCHEMA_FOR:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        root, provider_symbol, stype_in, dataset = self.resolve_symbology(
            symbol, contract_mode, lead_continuous=lead_continuous
        )
        cache_symbol = symbol.upper()

        if use_cache:
            missing = self.cache.missing_days(
                source=self.name, root=root, resolution=timeframe,
                symbol=cache_symbol, start=start, end=end,
            )
            if not missing:
                cached = self.cache.read(
                    source=self.name, root=root, resolution=timeframe,
                    symbol=cache_symbol, start=start, end=end,
                )
                if cached is not None and not cached.is_empty():
                    return cached

        frames = [
            self._fetch_chunk(dataset, provider_symbol, stype_in, c0, c1, timeframe)
            for c0, c1 in self._chunks(start, end, timeframe)
        ]
        frames = [f for f in frames if not f.is_empty()]
        if not frames:
            raise ValueError(
                f"Databento returned no {timeframe} data for {provider_symbol} "
                f"[{start:%Y-%m-%d} .. {end:%Y-%m-%d})"
            )
        raw = self._standardize(pl.concat(frames, how="vertical_relaxed"), timeframe)

        if timeframe == "tick":
            out = raw.sort("timestamp_utc")
        else:
            constant = parse_contract(symbol).code if contract_mode == "DATED" else None
            if "underlying_contract" in raw.columns:
                # provider returns raw symbols (NQH5) -> canonical (NQH25)
                ref = start.astimezone(UTC).date()
                raw = raw.with_columns(
                    pl.col("underlying_contract")
                    .map_elements(
                        lambda s: parse_contract(s, reference=ref).code,
                        return_dtype=pl.String,
                    )
                    .alias("underlying_contract")
                )
                # a DATED request must yield exactly the requested contract;
                # a CONTINUOUS one, only this root's contracts
                raw = raw.filter(
                    pl.col("underlying_contract") == constant
                    if constant
                    else pl.col("underlying_contract").str.starts_with(root)
                )
                if raw.is_empty():
                    raise ValueError(
                        f"Databento returned no rows for {symbol} after contract filtering"
                    )
            out = normalize_candles(
                raw, clock=self.clock, symbol=cache_symbol, root_symbol=root,
                source=self.name, resolution=timeframe,
                underlying_contract=constant,
            )
        if use_cache and not out.is_empty() and timeframe != "tick":
            self.cache.write(
                out, source=self.name, root=root, resolution=timeframe, symbol=cache_symbol
            )
        return out
