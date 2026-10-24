"""Droid's workspace: a folder OUTSIDE the repo holding exactly what Droid may see.

~/.skillsmith/droid_ws/<run_id>/
  AGENTS.md, API.md   rules + skill API (from droid_template/)
  skill.py            the skill under repair: the ONLY file Droid may edit
  failure.md          this round's prompt + evidence package (evidence v2, repair/report.py)
  history/round_N.md  every earlier evidence package
  frames/             annotated key frames / expected-vs-actual images the evidence links to
  scene.json          detected objects
  snapshot.jpg        camera frame at failure (if any)
  LESSONS.md          lessons from earlier tasks (copy of skills/lessons.md)
  library/*.py        read-only copies of saved skills
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

from skillsmith.config import REPO_ROOT
from skillsmith.repair.protocols import AuthorContext
from skillsmith.repair.report import evidence_images

TEMPLATE_DIR = REPO_ROOT / "droid_template"
AGENTS_FILES = {"v1": "AGENTS_v1.md", "v2": "AGENTS.md"}  # evidence version -> rules Droid reads as AGENTS.md


def template_file(name: str, evidence: str = "v2") -> Path:
    """droid_template/<name>; AGENTS.md is the v1 rules (no evidence v2 sections) in v1 mode."""
    return TEMPLATE_DIR / (AGENTS_FILES[evidence] if name == "AGENTS.md" else name)


def workspace_dir(cfg, run_id: str) -> Path:
    return cfg.path(cfg.repair.droid_ws_root) / run_id


def lessons_path(cfg) -> Path:
    return cfg.path(cfg.repair.get("lessons_file", "skills/lessons.md"))


def prepare_workspace(cfg, source: str, prompt_md: str, report_md: str, ctx: AuthorContext,
                      evidence: str = "v2") -> Path:
    ws = workspace_dir(cfg, ctx.run_id)
    (ws / "history").mkdir(parents=True, exist_ok=True)
    for name in ("AGENTS.md", "API.md"):
        shutil.copy(template_file(name, evidence), ws / name)
    (ws / "skill.py").write_text(source)
    (ws / "failure.md").write_text(localise_images(prompt_md, ws, ctx.round))
    (ws / "history" / f"round_{ctx.round}.md").write_text(localise_images(report_md, ws, ctx.round))
    if ctx.scene is not None:
        (ws / "scene.json").write_text(json.dumps(ctx.scene.to_dict(), indent=2))
    if ctx.snapshot_path and Path(ctx.snapshot_path).exists():
        shutil.copy(ctx.snapshot_path, ws / "snapshot.jpg")
    lessons = lessons_path(cfg)
    (ws / "LESSONS.md").write_text(lessons.read_text() if lessons.exists() else "# Lessons\n\n(none yet)\n")
    lib = ws / "library"
    if lib.exists():
        shutil.rmtree(lib, onerror=_force_remove)
    lib.mkdir()
    for name, src in ctx.library.items():
        p = lib / f"{name}.py"
        p.write_text(src)
        p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return ws


def localise_images(md: str, ws: Path, rnd: int) -> str:
    """Copy every image the evidence links to into frames/ and point the links there, so Droid
    reads key frames from its own workspace. Missing files keep their link (and say so)."""
    for path in evidence_images(md):
        src = Path(path)
        if not src.is_file():
            md = md.replace(f"]({path})", f"]({path}) _(missing)_")
            continue
        dest = ws / "frames" / f"round{rnd}_{src.name}"
        dest.parent.mkdir(exist_ok=True)
        shutil.copy(src, dest)
        md = md.replace(f"]({path})", f"](frames/{dest.name})")
    return md


def _force_remove(func, path, _exc):
    Path(path).chmod(stat.S_IWUSR | stat.S_IRUSR)
    func(path)
