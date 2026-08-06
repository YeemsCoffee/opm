from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from fvg_backtest.cli.main import build_parser, main

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data"
NQ_PARQUET = SAMPLE / "NQ_1m_2025Q1.parquet"
NQ_CSV = SAMPLE / "csv" / "NQH25_1m_2025-01-06.csv"

pytestmark = pytest.mark.skipif(
    not NQ_PARQUET.exists(),
    reason="run scripts/make_sample_data.py to generate sample_data/",
)


def test_parser_exposes_documented_commands():
    parser = build_parser()
    # every command in the README is reachable
    for argv in (
        ["contracts", "--root", "NQ"],
        ["download", "--provider", "databento", "--symbol", "NQ",
         "--start", "2025-01-01", "--end", "2025-12-31", "--resolution", "1m"],
        ["run", "--symbol", "NQ", "--config", "config/nq.yaml"],
        ["compare", "--symbols", "NQ", "MNQ", "--start", "2025-01-01", "--end", "2025-12-31"],
        ["report", "--run-id", "abc"],
        ["walkforward", "--symbol", "NQ"],
        ["cache"],
        ["credentials"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func)


def test_contracts_command(capsys):
    assert main(["contracts", "--root", "NQ", "--start", "2025-01-01", "--end", "2025-06-30"]) == 0
    out = capsys.readouterr().out
    assert "NQH25" in out and "NQH5" in out          # canonical and provider symbols
    assert "2025-03-21" in out                       # third-Friday expiration
    assert "20.0" in out                             # point value from config


def test_contracts_rejects_unknown_root(capsys):
    assert main(["contracts", "--root", "ES"]) == 2
    assert "unknown root" in capsys.readouterr().err


def test_run_command_writes_a_run(tmp_path, capsys):
    code = main([
        "run", "--symbol", "NQ", "--provider", "parquet", "--path", str(NQ_PARQUET),
        "--start", "2025-01-06", "--end", "2025-01-31",
        "--run-id", "cli-test", "--runs-dir", str(tmp_path),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "sessions_processed" in out
    base = tmp_path / "cli-test"
    assert (base / "summary.json").exists()
    summary = json.loads((base / "summary.json").read_text())
    assert summary["instrument"] == "NQ"
    assert summary["trades"] > 0
    trades = pl.read_parquet(base / "trades.parquet")
    assert "result_r" in trades.columns


def test_run_with_dated_contract(tmp_path):
    assert main([
        "run", "--contract", "NQH25", "--provider", "parquet", "--path", str(NQ_PARQUET),
        "--start", "2025-01-06", "--end", "2025-01-17",
        "--run-id", "dated", "--runs-dir", str(tmp_path),
    ]) == 0
    summary = json.loads((tmp_path / "dated" / "summary.json").read_text())
    assert summary["contract_mode"] == "DATED"
    assert summary["contract"] == "NQH25"
    contracts = pl.read_parquet(tmp_path / "dated" / "contracts.parquet")
    assert contracts["underlying_contract"].unique().to_list() == ["NQH25"]


def test_run_from_csv_provider(tmp_path):
    assert main([
        "run", "--contract", "NQH25", "--provider", "csv", "--path", str(NQ_CSV),
        "--start", "2025-01-06", "--end", "2025-01-06",
        "--run-id", "csv-run", "--runs-dir", str(tmp_path),
    ]) == 0
    summary = json.loads((tmp_path / "csv-run" / "summary.json").read_text())
    assert summary["sessions_processed"] == 1


def test_report_command(tmp_path, capsys):
    main([
        "run", "--symbol", "NQ", "--provider", "parquet", "--path", str(NQ_PARQUET),
        "--start", "2025-01-06", "--end", "2025-01-31",
        "--run-id", "rep", "--runs-dir", str(tmp_path),
    ])
    capsys.readouterr()
    assert main(["report", "--run-id", "rep", "--runs-dir", str(tmp_path),
                 "--by", "order_kind"]) == 0
    out = capsys.readouterr().out
    assert "expectancy_r" in out
    assert "ORIGINAL" in out
    assert "report.html" in out


def test_report_missing_run(tmp_path, capsys):
    assert main(["report", "--run-id", "nope", "--runs-dir", str(tmp_path)]) == 2
    assert "no such run" in capsys.readouterr().err


def test_compare_command(tmp_path, capsys):
    code = main([
        "compare", "--symbols", "NQ", "MNQ", "--provider", "parquet",
        "--path", str(SAMPLE), "--start", "2025-01-06", "--end", "2025-01-31",
        "--runs-dir", str(tmp_path),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "per instrument" in out
    assert "same sessions only" in out
    assert "NQ" in out and "MNQ" in out


def test_cache_command_on_empty_cache(tmp_path, capsys):
    assert main(["cache", "--cache-dir", str(tmp_path / "nothing")]) == 0
    assert "is empty" in capsys.readouterr().out


def test_credentials_never_prints_the_key(monkeypatch, capsys):
    monkeypatch.setenv("DATABENTO_API_KEY", "db-SUPERSECRETVALUE12345678")
    assert main(["credentials"]) == 0
    out = capsys.readouterr().out
    assert "SUPERSECRETVALUE" not in out
    assert json.loads(out)["present"] is True

    monkeypatch.delenv("DATABENTO_API_KEY")
    assert main(["credentials"]) == 1


def test_download_to_parquet(tmp_path, capsys):
    dest = tmp_path / "out.parquet"
    code = main([
        "download", "--provider", "parquet", "--path", str(NQ_PARQUET),
        "--symbol", "NQH25", "--start", "2025-01-06", "--end", "2025-01-08",
        "--resolution", "1m", "--out", str(dest),
    ])
    assert code == 0
    assert dest.exists()
    df = pl.read_parquet(dest)
    assert df["underlying_contract"].unique().to_list() == ["NQH25"]
    assert "rows" in capsys.readouterr().out


def test_errors_are_reported_not_raised(capsys):
    assert main([
        "run", "--symbol", "NQ", "--provider", "csv", "--path", "/does/not/exist.csv",
        "--start", "2025-01-06", "--end", "2025-01-07",
    ]) == 1
    assert "error:" in capsys.readouterr().err
