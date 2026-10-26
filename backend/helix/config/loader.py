"""Load HelixConfig from defaults, a YAML/JSON file or a dict, and gate startup."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from pydantic import ValidationError

from .schema import ConfigError, HelixConfig


class ConfigLoadError(Exception):
    """Raised when a config document cannot be parsed or has wrong types/keys."""

    def __init__(self, errors: list[ConfigError]):
        self.errors = errors
        super().__init__("; ".join(str(e) for e in errors))


class ConfigValidationError(Exception):
    """Raised by `ensure_valid` when one or more settings are out of range."""

    def __init__(self, errors: list[ConfigError]):
        self.errors = errors
        super().__init__("invalid HELIX config: " + "; ".join(str(e) for e in errors))


def from_mapping(data: Optional[Mapping[str, Any]] = None) -> HelixConfig:
    """Build a config from a (possibly partial, nested) mapping over the defaults.

    Pydantic reports every type error and unknown key at once; ranges are not
    checked here (call `validate()` / `ensure_valid`).
    """
    try:
        return HelixConfig.model_validate(dict(data or {}))
    except ValidationError as exc:
        raise ConfigLoadError(
            [
                ConfigError(".".join(str(p) for p in err["loc"]), err.get("input"), err["msg"])
                for err in exc.errors()
            ]
        ) from exc


def load_config(path: Optional[Union[str, Path]] = None) -> HelixConfig:
    """Load from a .yaml/.yml/.json file, or return defaults when `path` is None."""
    if path is None:
        return HelixConfig()
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(text) if text.strip() else {}
        else:
            data = yaml.safe_load(text) or {}
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigLoadError([ConfigError(str(p), None, f"unparseable config file: {exc}")]) from exc
    if not isinstance(data, Mapping):
        raise ConfigLoadError([ConfigError(str(p), type(data).__name__, "top level must be a mapping")])
    return from_mapping(data)


def with_updates(cfg: HelixConfig, updates: Mapping[str, Any]) -> HelixConfig:
    """Return a copy with dotted-path updates applied (no range checking)."""
    data = cfg.model_dump()
    for dotted, value in updates.items():
        *parents, leaf = dotted.split(".")
        node = data
        for key in parents:
            node = node[key]
        node[leaf] = value
    return from_mapping(data)


def ensure_valid(cfg: HelixConfig) -> HelixConfig:
    """Startup gate: raise with every error iff `cfg.validate()` is non-empty."""
    errors = cfg.validate()
    if errors:
        raise ConfigValidationError(errors)
    return cfg


def startup_config(path: Optional[Union[str, Path]] = None) -> HelixConfig:
    """Load and validate; on any error log all of them to stderr and exit non-zero."""
    try:
        return ensure_valid(load_config(path))
    except (ConfigLoadError, ConfigValidationError) as exc:
        for e in exc.errors:
            print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2) from exc
