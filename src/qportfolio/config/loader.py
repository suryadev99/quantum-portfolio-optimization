"""YAML config loading with schema validation (F9-R4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from qportfolio.config.schema import Config
from qportfolio.utils.errors import ConfigError

_SECRET_KEYS = {"token", "api_key", "apikey", "password", "secret"}


def _reject_secrets(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in _SECRET_KEYS:
                raise ConfigError(
                    f"Secret-looking key {path}{k!r} in config. Credentials must come from "
                    "environment variables (QISKIT_IBM_TOKEN) or a saved account, never config."
                )
            _reject_secrets(v, f"{path}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_secrets(v, f"{path}{i}.")


def config_from_dict(raw: dict[str, Any]) -> Config:
    _reject_secrets(raw)
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration:\n{exc}") from exc


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if overrides:
        raw = deep_update(raw, overrides)
    return config_from_dict(raw)


def deep_update(base: dict[str, Any], upd: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_update(out[k], v)
        else:
            out[k] = v
    return out


def dump_config(cfg: Config) -> dict[str, Any]:
    return cfg.model_dump(mode="json")
