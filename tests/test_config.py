from __future__ import annotations

import pytest

from fvg_backtest.config import AppConfig, load_config, config_to_yaml
from fvg_backtest.config.loader import save_config


def test_defaults_are_spec_defaults(config: AppConfig):
    assert config.significance.type_a.minimum_gap_atr == 0.10
    assert config.significance.type_a.minimum_preservation_ratio == 0.50
    assert config.significance.type_b.prior_wick_lookback_minutes == 15
    assert config.significance.type_b.minimum_wick_atr == 0.15
    assert config.significance.type_b.minimum_wick_share == 0.40
    assert config.significance.type_b.minimum_fvg_overlap_ratio == 0.25
    assert config.atr.length == 20
    assert config.sessions.cash_open == "09:30"
    assert config.entries.model == "PROXIMAL_EDGE"
    assert config.inversion.stop_model == "OPPOSITE_FVG_EDGE_PLUS_BUFFER"
    assert config.execution.mode == "ONE_MINUTE_CONSERVATIVE"
    assert config.rolls.back_adjust is False


def test_instrument_metadata_editable_defaults(config: AppConfig):
    nq = config.instruments["NQ"]
    mnq = config.instruments["MNQ"]
    assert nq.tick_size == 0.25 and nq.point_value == 20.0
    assert nq.tick_value == 5.0
    assert mnq.tick_size == 0.25 and mnq.point_value == 2.0
    assert mnq.tick_value == 0.5
    assert nq.exchange == "CME" == mnq.exchange
    assert nq.databento.dataset == "GLBX.MDP3"
    # cost settings may differ between NQ and MNQ
    assert nq.costs.commission_per_contract != mnq.costs.commission_per_contract


def test_yaml_roundtrip_and_overlay(tmp_path, config: AppConfig):
    p = tmp_path / "cfg.yaml"
    save_config(config, p)
    loaded = load_config(p)
    assert loaded == config

    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "instrument: MNQ\nsignificance:\n  type_a:\n    minimum_gap_atr: 0.2\n"
    )
    merged = load_config(p, overlay)
    assert merged.instrument == "MNQ"
    assert merged.significance.type_a.minimum_gap_atr == 0.2
    # untouched keys survive the merge
    assert merged.significance.type_a.minimum_preservation_ratio == 0.50


def test_dated_mode_requires_contract():
    with pytest.raises(ValueError):
        AppConfig(contract_mode="DATED")
    cfg = AppConfig(contract_mode="DATED", contract="NQH25")
    assert cfg.contract == "NQH25"


def test_dotted_path_access(config: AppConfig):
    assert config.get_by_path("significance.type_a.minimum_gap_atr") == 0.10
    config.set_by_path("significance.type_a.minimum_gap_atr", 0.3)
    assert config.significance.type_a.minimum_gap_atr == 0.3


def test_yaml_export_contains_all_sections(config: AppConfig):
    text = config_to_yaml(config)
    for key in ("instruments", "sessions", "significance", "liquidity", "labels", "walkforward"):
        assert key in text
