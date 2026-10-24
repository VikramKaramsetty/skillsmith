"""Saved skills: skills/library/<name>.py + skills/registry.json, with history.

registry.json: {"stack": {"file": "stack.py", "version": 3, "uses": ["pick_place"], "successes": 2}}
Old versions are kept under skills/history/<name>/v<N>.py.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Dict, List


class SkillLibrary:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.dir.parent / "registry.json"
        self.history_dir = self.dir.parent / "history"
        self._registry = self._load_registry()

    def _load_registry(self) -> dict:
        if self.registry_path.exists():
            text = self.registry_path.read_text().strip()
            if text:
                return json.loads(text)
        return {}

    def _write_registry(self) -> None:
        self.registry_path.write_text(json.dumps(self._registry, indent=2, sort_keys=True) + "\n")

    def names(self) -> List[str]:
        return sorted(p.stem for p in self.dir.glob("*.py"))

    def has(self, name: str) -> bool:
        return (self.dir / f"{name}.py").exists()

    def load(self, name: str) -> str:
        return (self.dir / f"{name}.py").read_text()

    def version(self, name: str) -> int:
        return self._registry.get(name, {}).get("version", 1 if self.has(name) else 0)

    def sources(self) -> Dict[str, str]:
        return {n: self.load(n) for n in self.names()}

    def save(self, name: str, source: str) -> int:
        """Save a working skill; returns its new version."""
        version = self.version(name) + 1
        if self.has(name):
            old = self.history_dir / name / f"v{version - 1}.py"
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text(self.load(name))
        (self.dir / f"{name}.py").write_text(source)
        entry = self._registry.setdefault(name, {"successes": 0})
        entry.update({"file": f"{name}.py", "version": version, "uses": _uses(source)})
        entry["successes"] = entry.get("successes", 0) + 1
        self._write_registry()
        return version

    def record_success(self, name: str) -> None:
        entry = self._registry.setdefault(name, {"file": f"{name}.py", "version": 1, "successes": 0})
        entry["successes"] = entry.get("successes", 0) + 1
        self._write_registry()


def _uses(source: str) -> List[str]:
    """Read SKILL['uses'] from the source without executing it."""
    try:
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "SKILL" for t in node.targets):
                return list(ast.literal_eval(node.value).get("uses", []))
    except (SyntaxError, ValueError, AttributeError):
        pass
    return []
