"""Droid's workspace: a folder OUTSIDE the repo holding exactly what Droid may see.

~/.skillsmith/droid_ws/<run_id>/
  AGENTS.md, API.md   rules + skill API (from droid_template/)
  skill.py            the skill under repair: the ONLY file Droid may edit
  failure.md          this round's prompt + evidence package (evidence v2, repair/report.py)
  history/round_N.md  every earlier evidence package
  frames/             annotated key frames / expected-vs-actual images of this attempt: only paths
                      from the AttemptResults that resolve under runs_dir (or the render dir)
  scene.json          detected objects
  snapshot.jpg        camera frame at failure (if any)
  LESSONS.md          lessons from earlier tasks (copy of skills/lessons.md)
  library/*.py        read-only copies of saved skills
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from skillsmith.config import REPO_ROOT
from skillsmith.events import EventBus
from skillsmith.repair.protocols import AuthorContext
from skillsmith.repair.report import IMAGE_LINK_RE, attempt_images

TEMPLATE_DIR = REPO_ROOT / "droid_template"
AGENTS_FILES = {"v1": "AGENTS_v1.md", "v2": "AGENTS.md"}  # evidence version -> rules Droid reads as AGENTS.md


def template_file(name: str, evidence: str = "v2") -> Path:
    """droid_template/<name>; AGENTS.md is the v1 rules (no evidence v2 sections) in v1 mode."""
    return TEMPLATE_DIR / (AGENTS_FILES[evidence] if name == "AGENTS.md" else name)


def workspace_dir(cfg, run_id: str) -> Path:
    return cfg.path(cfg.repair.droid_ws_root) / run_id


def lessons_path(cfg) -> Path:
    return cfg.path(cfg.repair.get("lessons_file", "skills/lessons.md"))


def image_roots(cfg) -> List[Path]:
    """Folders evidence images may come from: runs_dir, plus repair.render_dir if configured."""
    roots = [cfg.path(cfg.repair.get("runs_dir", "runs"))]
    render = cfg.repair.get("render_dir")
    if render:
        roots.append(cfg.path(render))
    return [Path(r).expanduser().resolve() for r in roots]


def under_roots(path: str, roots: Sequence[Path]) -> bool:
    """True if `path` (symlinks resolved) lies inside one of `roots`."""
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return any(os.path.commonpath([str(r), str(target)]) == str(r) for r in roots)


def split_by_roots(paths: Sequence[str], roots: Sequence[Path]) -> Tuple[List[str], List[str]]:
    """(allowed, rejected): `paths` inside `roots`, and the rest."""
    allowed = [p for p in paths if under_roots(p, roots)]
    return allowed, [p for p in paths if p not in allowed]


def evidence_paths(cfg, ctx: AuthorContext, bus: Optional[EventBus] = None) -> List[str]:
    """This round's evidence images: from ctx.attempt / ctx.expected only, inside image_roots.
    Rejected paths are reported as an `error` event, never silently dropped."""
    allowed, rejected = split_by_roots(attempt_images(ctx.attempt, ctx.expected), image_roots(cfg))
    if rejected and bus is not None:
        bus.emit("error", where="droid.images", round=ctx.round,
                 message=f"evidence images outside runs_dir not sent to Droid: {rejected}")
    return allowed


def prepare_workspace(cfg, source: str, prompt_md: str, report_md: str, ctx: AuthorContext,
                      evidence: str = "v2", bus: Optional[EventBus] = None) -> Path:
    ws = workspace_dir(cfg, ctx.run_id)
    images = evidence_paths(cfg, ctx, bus)
    (ws / "history").mkdir(parents=True, exist_ok=True)
    for name in ("AGENTS.md", "API.md"):
        shutil.copy(template_file(name, evidence), ws / name)
    (ws / "skill.py").write_text(source)
    (ws / "failure.md").write_text(localise_images(prompt_md, ws, ctx.round, images))
    (ws / "history" / f"round_{ctx.round}.md").write_text(localise_images(report_md, ws, ctx.round, images))
    if ctx.scene is not None:
        (ws / "scene.json").write_text(json.dumps(ctx.scene.to_dict(), indent=2))
    if ctx.snapshot_path and Path(ctx.snapshot_path).is_file() and under_roots(ctx.snapshot_path, image_roots(cfg)):
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


def localise_images(md: str, ws: Path, rnd: int, images: Sequence[str]) -> str:
    """Copy the evidence images (`images`, from evidence_paths) into frames/ and point their links
    there, so Droid reads key frames from its own workspace. Missing files keep their link (and say
    so). Any other image link in the text is removed: nothing outside `images` is ever copied."""
    allowed = set(images)

    def swap(m: re.Match) -> str:
        path = m.group(1)
        if path not in allowed:
            return "_(image link removed: not part of this attempt's evidence)_"
        src = Path(path)
        if not src.is_file():
            return f"{m.group(0)} _(missing)_"
        dest = ws / "frames" / f"round{rnd}_{src.name}"
        dest.parent.mkdir(exist_ok=True)
        shutil.copy(src, dest)
        return m.group(0)[:-len(path) - 1] + f"frames/{dest.name})"

    return IMAGE_LINK_RE.sub(swap, md)


def _force_remove(func, path, _exc):
    Path(path).chmod(stat.S_IWUSR | stat.S_IRUSR)
    func(path)
