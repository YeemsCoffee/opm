from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl
import pytest

from fvg_backtest.data.file_provider import FileProvider
from fvg_backtest.data.synthetic import SyntheticProvider, generate_session_bars

UTC = timezone.utc


def _fvg_exists_bull(c1_high, c3_low):
    return c3_low > c1_high


def _cash_fvgs(raw: pl.DataFrame) -> list[tuple[str, str]]:
    """(HH:MM of candle 3, direction) for every FVG completing at/after 9:30."""
    df = raw.with_columns(
        pl.col("timestamp_utc").dt.convert_time_zone("America/New_York").alias("ny")
    )
    rows = df.select("high", "low").rows()
    stamps = [str(t)[11:16] for t in df["ny"]]
    out = []
    for i in range(2, len(rows)):
        h1, l1 = rows[i - 2]
        h3, l3 = rows[i]
        if stamps[i - 2] < "09:30":
            continue
        if l3 > h1:
            out.append((stamps[i], "bull"))
        elif h3 < l1:
            out.append((stamps[i], "bear"))
    return out


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("bullish_clean", ("09:33", "bull")),
        ("bearish_clean", ("09:33", "bear")),
        ("bullish_inversion", ("09:33", "bull")),
        ("ranging", ("09:33", "bull")),
        ("sweaty_win", ("09:33", "bull")),
    ],
)
def test_scenarios_plant_the_first_cash_fvg_at_933(clock, jan6, kind, expected):
    found = _cash_fvgs(generate_session_bars(jan6, kind, clock))
    assert found[0] == expected


def test_no_fvg_scenario_has_no_cash_fvg(clock, jan6):
    assert _cash_fvgs(generate_session_bars(jan6, "no_fvg", clock)) == []


def test_planted_bullish_fvg_exists(clock, jan6, config):
    raw = generate_session_bars(jan6, "bullish_clean", clock)
    open_ny, _ = clock.session_bounds(jan6)
    # candles 9:31, 9:32, 9:33 (minutes 931..933 from 18:00 open)
    df = raw.with_columns(pl.col("timestamp_utc").dt.convert_time_zone("America/New_York").alias("ny"))
    c1 = df.filter(pl.col("ny").dt.hour().eq(9) & pl.col("ny").dt.minute().eq(31)).row(0, named=True)
    c3 = df.filter(pl.col("ny").dt.hour().eq(9) & pl.col("ny").dt.minute().eq(33)).row(0, named=True)
    assert _fvg_exists_bull(c1["high"], c3["low"])
    assert c3["low"] - c1["high"] == pytest.approx(4.0)  # zone [B+5, B+9]


def test_synthetic_provider_1m(clock, config):
    prov = SyntheticProvider(clock, config.instruments, schedule={date(2025, 1, 6): "bullish_clean"})
    df = prov.get_bars(
        "NQ",
        datetime(2025, 1, 5, 20, 0, tzinfo=UTC),
        datetime(2025, 1, 6, 22, 30, tzinfo=UTC),
        "1m",
        "CONTINUOUS",
    )
    assert df.height > 1000
    assert set(df["underlying_contract"].unique().to_list()) == {"NQH25"}
    assert df["resolution"][0] == "1m"


def test_synthetic_provider_1s_aggregates_to_1m(clock, config):
    prov = SyntheticProvider(clock, config.instruments, schedule={date(2025, 1, 6): "bullish_clean"})
    start = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)  # 9:30 NY
    end = datetime(2025, 1, 6, 14, 40, tzinfo=UTC)
    m1 = prov.get_bars("NQ", start, end, "1m", "CONTINUOUS")
    s1 = prov.get_bars("NQ", start, end, "1s", "CONTINUOUS")
    assert s1.height == m1.height * 60
    agg = (
        s1.group_by_dynamic("timestamp_utc", every="1m")
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
        )
        .sort("timestamp_utc")
    )
    joined = agg.join(m1, on="timestamp_utc", suffix="_m")
    assert (joined["open"] == joined["open_m"]).all()
    assert (joined["high"] == joined["high_m"]).all()
    assert (joined["low"] == joined["low_m"]).all()
    assert (joined["close"] == joined["close_m"]).all()


def test_synthetic_ticks(clock, config):
    prov = SyntheticProvider(clock, config.instruments, schedule={date(2025, 1, 6): "bullish_clean"})
    start = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
    end = datetime(2025, 1, 6, 14, 32, tzinfo=UTC)
    ticks = prov.get_bars("NQ", start, end, "tick", "CONTINUOUS")
    assert ticks.columns == ["timestamp_utc", "price", "size", "underlying_contract"]
    assert ticks.height == 120


def test_file_provider_csv_and_parquet_roundtrip(tmp_path, clock, config, jan6):
    raw = generate_session_bars(jan6, "bullish_clean", clock)
    csv_path = tmp_path / "NQH25_1m.csv"
    pq_path = tmp_path / "NQH25_1m.parquet"
    raw.write_csv(csv_path)
    raw.write_parquet(pq_path)

    start = datetime(2025, 1, 5, 23, 0, tzinfo=UTC)
    end = datetime(2025, 1, 6, 22, 0, tzinfo=UTC)

    for path in (csv_path, pq_path):
        prov = FileProvider(path, clock, config.instruments)
        df = prov.get_bars("NQH25", start, end, "1m", "DATED")
        assert df["underlying_contract"].unique().to_list() == ["NQH25"]
        assert df["symbol"][0] == "NQH25"
        assert df.height > 500


def test_file_provider_contract_from_filename(tmp_path, clock, config, jan6):
    raw = generate_session_bars(jan6, "no_fvg", clock).drop("underlying_contract")
    p = tmp_path / "mnqh25_data.csv"
    raw.write_csv(p)
    prov = FileProvider(p, clock, config.instruments)
    df = prov.get_bars(
        "MNQH25",
        datetime(2025, 1, 5, 23, 0, tzinfo=UTC),
        datetime(2025, 1, 6, 22, 0, tzinfo=UTC),
        "1m",
        "DATED",
    )
    assert df["underlying_contract"].unique().to_list() == ["MNQH25"]
    assert df["root_symbol"][0] == "MNQ"


def test_file_provider_continuous_needs_contract_column(tmp_path, clock, config, jan6):
    raw = generate_session_bars(jan6, "no_fvg", clock).drop("underlying_contract")
    p = tmp_path / "data.csv"  # no contract hint in the name either
    raw.write_csv(p)
    prov = FileProvider(p, clock, config.instruments)
    with pytest.raises(ValueError, match="underlying_contract"):
        prov.get_bars(
            "NQ",
            datetime(2025, 1, 5, 23, 0, tzinfo=UTC),
            datetime(2025, 1, 6, 22, 0, tzinfo=UTC),
            "1m",
            "CONTINUOUS",
        )


def test_file_provider_nq_excludes_mnq_rows(tmp_path, clock, config, jan6):
    nq = generate_session_bars(jan6, "no_fvg", clock, contract="NQH25")
    mnq = generate_session_bars(jan6, "no_fvg", clock, contract="MNQH25")
    pl.concat([nq, mnq]).write_parquet(tmp_path / "both.parquet")
    prov = FileProvider(tmp_path / "both.parquet", clock, config.instruments)
    df = prov.get_bars(
        "NQ",
        datetime(2025, 1, 5, 23, 0, tzinfo=UTC),
        datetime(2025, 1, 6, 22, 0, tzinfo=UTC),
        "1m",
        "CONTINUOUS",
    )
    assert df["underlying_contract"].unique().to_list() == ["NQH25"]
