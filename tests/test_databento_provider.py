"""Databento provider tests — no network: a fake client replays fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import polars as pl
import pytest

from fvg_backtest.data.cache import DataCache
from fvg_backtest.data.databento_provider import (
    DatabentoCredentialError,
    DatabentoProvider,
    credential_status,
)

UTC = timezone.utc


class FakeStore:
    def __init__(self, pdf: pd.DataFrame) -> None:
        self._pdf = pdf

    def to_df(self) -> pd.DataFrame:
        return self._pdf


class FakeClient:
    """Records requests and replays 1-minute bars for two dated contracts."""

    def __init__(self, fail_times: int = 0, error: str = "rate limit exceeded (429)") -> None:
        self.requests: list[dict] = []
        self.fail_times = fail_times
        self.error = error
        self.timeseries = self
        self.symbology = self

    def get_range(self, **kw):
        self.requests.append(kw)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(self.error)
        start, end = kw["start"], kw["end"]
        rows = []
        ts = start
        while ts < end:
            for sym, base in (("NQH5", 21000.0), ("NQM5", 21100.0)):
                rows.append(
                    {
                        "ts_event": pd.Timestamp(ts).tz_convert("UTC"),
                        "open": base, "high": base + 2, "low": base - 2, "close": base + 1,
                        "volume": 100.0, "symbol": sym,
                    }
                )
            ts += timedelta(minutes=1)
        return FakeStore(pd.DataFrame(rows).set_index("ts_event"))

    def resolve(self, **kw):
        return {"result": {"NQ.FUT": [{"s": "NQH5"}, {"s": "NQM5"}]}}


@pytest.fixture()
def provider(clock, config, tmp_path):
    cfg = config.data.model_copy(update={"cache_dir": str(tmp_path / "cache")})
    return DatabentoProvider(
        clock, config.instruments, cfg, client=FakeClient(), sleeper=lambda s: None
    )


def test_credential_status_never_echoes_key():
    st = credential_status("db-abcdefghijklmnopqrstuvwxyz")
    assert st["present"] and st["valid_format"]
    assert "abcdefghijklmnop" not in st["masked"]
    assert credential_status("")["present"] is False
    assert credential_status("nope")["valid_format"] is False


def test_missing_key_raises_only_on_use(clock, config, monkeypatch):
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    prov = DatabentoProvider(clock, config.instruments)  # constructing is fine
    with pytest.raises(DatabentoCredentialError, match="DATABENTO_API_KEY"):
        _ = prov.client


def test_symbology_mapping(provider):
    assert provider.resolve_symbology("NQH25", "DATED")[1:3] == ("NQH5", "raw_symbol")
    assert provider.resolve_symbology("NQ", "CONTINUOUS")[1:3] == ("NQ.FUT", "parent")
    assert provider.resolve_symbology("MNQ", "CONTINUOUS")[1:3] == ("MNQ.FUT", "parent")
    assert provider.resolve_symbology("NQ", "CONTINUOUS", lead_continuous=True)[1:3] == (
        "NQ.c.0", "continuous",
    )
    assert provider.resolve_symbology("NQH25", "DATED")[3] == "GLBX.MDP3"


def test_dated_request_returns_single_contract(provider):
    df = provider.get_bars(
        "NQH25",
        datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        datetime(2025, 1, 6, 14, 40, tzinfo=UTC),
        "1m", "DATED",
    )
    assert df["underlying_contract"].unique().to_list() == ["NQH25"]
    assert df["source"][0] == "databento"
    assert provider._client.requests[0]["stype_in"] == "raw_symbol"


def test_continuous_request_keeps_contracts_separate(provider):
    df = provider.get_bars(
        "NQ",
        datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        datetime(2025, 1, 6, 14, 35, tzinfo=UTC),
        "1m", "CONTINUOUS",
    )
    # parent symbology returns both contracts, each tagged — never merged
    assert sorted(df["underlying_contract"].unique().to_list()) == ["NQH25", "NQM25"]
    assert provider._client.requests[0]["stype_in"] == "parent"


def test_pagination_chunks_requests(provider):
    provider.get_bars(
        "NQH25",
        datetime(2025, 1, 6, tzinfo=UTC),
        datetime(2025, 3, 20, tzinfo=UTC),
        "1m", "DATED",
        use_cache=False,
    )
    # 1-minute data is chunked 30 days at a time -> 73 days => 3 requests
    assert len(provider._client.requests) == 3


def test_rate_limit_retry_with_backoff(clock, config, tmp_path):
    delays: list[float] = []
    cfg = config.data.model_copy(update={"cache_dir": str(tmp_path / "c")})
    prov = DatabentoProvider(
        clock, config.instruments, cfg,
        client=FakeClient(fail_times=2), sleeper=delays.append,
    )
    df = prov.get_bars(
        "NQH25",
        datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        datetime(2025, 1, 6, 14, 33, tzinfo=UTC),
        "1m", "DATED",
    )
    assert df.height == 3
    assert delays == [1.0, 2.0]  # exponential backoff


def test_non_retryable_error_propagates(clock, config, tmp_path):
    cfg = config.data.model_copy(update={"cache_dir": str(tmp_path / "c")})
    prov = DatabentoProvider(
        clock, config.instruments, cfg,
        client=FakeClient(fail_times=1, error="invalid symbol"), sleeper=lambda s: None,
    )
    with pytest.raises(RuntimeError, match="invalid symbol"):
        prov.get_bars(
            "NQH25",
            datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
            datetime(2025, 1, 6, 14, 33, tzinfo=UTC),
            "1m", "DATED",
        )


def test_cache_prevents_second_network_call(provider):
    args = (
        "NQH25",
        datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        datetime(2025, 1, 6, 14, 40, tzinfo=UTC),
        "1m", "DATED",
    )
    first = provider.get_bars(*args)
    calls = len(provider._client.requests)
    second = provider.get_bars(*args)
    assert len(provider._client.requests) == calls  # served from cache
    assert first.equals(second)


def test_cache_inventory(provider, tmp_path):
    provider.get_bars(
        "NQH25",
        datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        datetime(2025, 1, 6, 14, 40, tzinfo=UTC),
        "1m", "DATED",
    )
    inv = provider.cache.inventory_frame()
    assert inv.height == 1
    assert inv["symbol"][0] == "NQH25"
    assert inv["resolution"][0] == "1m"
    assert inv["rows"][0] == 10


def test_list_contracts_from_symbology(provider):
    got = provider.list_contracts(
        "NQ", datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 4, 1, tzinfo=UTC)
    )
    assert got == ["NQH5", "NQM5"]


def test_cache_missing_days(tmp_path):
    cache = DataCache(tmp_path)
    start = datetime(2025, 1, 6, tzinfo=UTC)
    end = datetime(2025, 1, 9, tzinfo=UTC)
    missing = cache.missing_days(
        source="databento", root="NQ", resolution="1m", symbol="NQH25", start=start, end=end
    )
    assert len(missing) == 3
