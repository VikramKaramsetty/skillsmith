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


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge `over` onto `base` (dicts merge, everything else replaces)."""
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_config(path: Optional[str] = None, profile: Optional[str] = None) -> Config:
    """robot.yaml (or `path` / $SKILLSMITH_CONFIG), then config/<profile>.yaml merged on top.
    `profile` defaults to $SKILLSMITH_PROFILE; e.g. "stage" for the demo (docs/DEMO.md). Several profiles,
    comma-separated, merge left to right: "mac,stage" = the arm on the Mac plus demo speed."""
    p = Path(path) if path else Path(os.environ.get("SKILLSMITH_CONFIG", DEFAULT_CONFIG))
    with open(p) as f:
        data = yaml.safe_load(f)
    profile = profile or os.environ.get("SKILLSMITH_PROFILE")
    if profile:
        for name in [n.strip() for n in profile.split(",") if n.strip()]:
            overlay = REPO_ROOT / "config" / f"{name}.yaml"
            if not overlay.exists():
                raise FileNotFoundError(f"no config profile '{name}' ({overlay})")
            data = _merge(data, yaml.safe_load(overlay.read_text()) or {})
        data["profile"] = profile
    return Config(data)
