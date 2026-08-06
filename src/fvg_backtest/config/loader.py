"""YAML loading / saving for :class:`AppConfig`.

Config files are *overlays*: ``load_config`` starts from schema defaults and
deep-merges every file in order, so ``config/nq.yaml`` only needs the keys it
changes.  ``save_config`` writes the fully-resolved config (used for
``runs/{run_id}/config.yaml`` reproducibility and dashboard export).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import AppConfig


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(*paths: str | Path, overrides: dict[str, Any] | None = None) -> AppConfig:
    """Build an AppConfig from schema defaults + YAML overlays + dict overrides."""
    merged: dict[str, Any] = {}
    for path in paths:
        if path is None:
            continue
        text = Path(path).read_text()
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        merged = _deep_merge(merged, data)
    if overrides:
        merged = _deep_merge(merged, overrides)
    return AppConfig.model_validate(merged)


def config_to_yaml(config: AppConfig) -> str:
    return yaml.safe_dump(
        config.model_dump(mode="json"), sort_keys=False, default_flow_style=False
    )


def save_config(config: AppConfig, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_to_yaml(config))
    return path
