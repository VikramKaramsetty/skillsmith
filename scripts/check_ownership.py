#!/usr/bin/env python3
"""Fail if a branch touches files outside its workstream's paths (.github/ownership.yml).

  python3 scripts/check_ownership.py --base origin/main            # local, before pushing
  python3 scripts/check_ownership.py --base HEAD^1 --branch ws2/x  # CI on the PR merge commit

Set OWNERSHIP_OVERRIDE=1 (CI does this for PRs labelled `lead-approved`) to skip.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_ownership(path: Path = ROOT / ".github" / "ownership.yml") -> dict:
    return yaml.safe_load(path.read_text())


def violations(branch: str, files: list[str], ownership: dict) -> list[str]:
    """Return human-readable problems; empty list means the branch stays in its lane."""
    prefix = branch.split("/", 1)[0]
    if prefix in ownership.get("lead_branches", []):
        return []
    ws = ownership["workstreams"].get(prefix)
    if ws is None:
        known = ", ".join([*ownership["workstreams"], *ownership.get("lead_branches", [])])
        return [f"branch '{branch}' must start with one of: {known} (e.g. ws2/my-change)"]
    allowed = [*ws["paths"], f"tests/{prefix}/"]
    return [f"{f} is outside {prefix} ({ws['title']}); allowed: {', '.join(allowed)}"
            for f in files if not any(f == p or f.startswith(p) for p in allowed)]


def changed_files(base: str, head: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...{head}"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def current_branch() -> str:
    return (os.environ.get("GITHUB_HEAD_REF")
            or subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="origin/main")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--branch", default=None)
    args = p.parse_args()
    if os.environ.get("OWNERSHIP_OVERRIDE") == "1":
        print("ownership: skipped (lead-approved)")
        return 0
    branch = args.branch or current_branch()
    problems = violations(branch, changed_files(args.base, args.head), load_ownership())
    for line in problems:
        print(f"ownership: {line}")
    if problems:
        print("\nMove those edits to the owning workstream, or ask the lead to label the PR `lead-approved`.")
        return 1
    print(f"ownership: ok ({branch})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
