"""Config loading. `Config` is a thin attribute-access wrapper over config/robot.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "robot.yaml"


class Config:
    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError as e:
            raise AttributeError(f"config has no key '{name}'") from e
        return Config(value) if isinstance(value, dict) else value

    def get(self, name: str, default: Any = None) -> Any:
        value = self._data.get(name, default)
        return Config(value) if isinstance(value, dict) else value

    def raw(self) -> dict:
        return self._data

    def path(self, value: str) -> Path:
        """Resolve a config path: ~ expands, relative paths are relative to the repo root."""
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else REPO_ROOT / p


def load_config(path: Optional[str] = None) -> Config:
    p = Path(path) if path else Path(os.environ.get("SKILLSMITH_CONFIG", DEFAULT_CONFIG))
    with open(p) as f:
        return Config(yaml.safe_load(f))
