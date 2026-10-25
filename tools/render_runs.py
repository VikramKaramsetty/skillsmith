"""Rebuild the sim renderings of recorded runs, for the dashboard's Iterations strip.

Each recorded run (runs/<id>/events.jsonl) holds every version Droid wrote as a `skill.diff`
and the scene the sim started from (`scene.observed`, source "sim"). This replays the diffs to
get each version's code, runs that version in today's MuJoCo sim on that scene, and saves
runs/<id>/sim/v<N>.mp4 (two angles), v<N>.jpg (the last frame: where it ended or failed) and
manifest.json, which the dashboard serves at GET /renders/<run_id>.

    uv run -q --extra dev --extra dashboard --extra kin --extra vision --extra sim --extra droid \
      python tools/render_runs.py [run_id ...] [--all] [--force] [--profile scratch]

No run ids = every run with a sim attempt on a real-table scene (cup tasks, not the fake stack demo).
The sim has changed since some runs were recorded, so the manifest keeps both outcomes:
`recorded` (what the gate said then) and `rerun` (what today's sim says).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterator, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FPS, STEPS_PER_FRAME = 30, 30  # 2 ms sim step: 60 ms of sim time per frame, ~1.8x real time
SIZE = (360, 480)              # per angle (h, w)


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def apply_diff(old: str, diff: str) -> str:
    """Apply one unified diff (as skill.diff records it) to `old`. An empty diff = unchanged."""
    if not diff.strip():
        return old
    src, out, pos = old.splitlines(), [], 0
    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        m = HUNK.match(lines[i])
        i += 1
        if not m:
            continue
        start = int(m.group(1)) - (0 if m.group(2) == "0" else 1)
        out.extend(src[pos:start])
        pos = start
        while i < len(lines) and not lines[i].startswith("@@"):
            line = lines[i]
            i += 1
            if line.startswith("\\"):
                continue  # "\ No newline at end of file"
            tag, text = (line[0], line[1:]) if line else (" ", "")
            if tag in " -":
                if pos >= len(src) or src[pos] != text:
                    raise ValueError(f"diff does not apply at line {pos + 1}: {text!r}")
                pos += 1
            if tag in " +":
                out.append(text)
    out.extend(src[pos:])
    return "\n".join(out) + "\n"


def versions(events: list[dict]) -> dict[int, str]:
    """Version number -> the skill's source, rebuilt from the run's skill.diff events."""
    code, out = "", {}
    for e in events:
        if e["type"] == "skill.diff":
            d = e["data"]
            code = apply_diff(code if d.get("old_version") is not None else "", d.get("diff") or "")
            out[int(d["new_version"])] = code
    return out


def sim_attempts(events: list[dict]) -> Iterator[dict]:
    """Each sim attempt: version, the scene the sim started from, and the recorded outcome."""
    current: Optional[dict] = None
    for e in events:
        d = e.get("data") or {}
        if e["type"] == "attempt.started":
            current = {"version": d.get("version"), "scene": None, "recorded": None} if d.get("env") == "sim" else None
        elif current is not None and e["type"] == "scene.observed" and current["scene"] is None:
            scene = d.get("scene") or {}
            if scene.get("source") == "sim":
                current["scene"] = scene
        elif current is not None and e["type"] == "attempt.finished":
            current["recorded"] = {"ok": bool(d.get("ok")), "reason": d.get("reason") or d.get("error") or ""}
            yield current
            current = None


def task_of(events: list[dict]) -> dict:
    return next(((e["data"].get("task") or {}) for e in events if e["type"] == "run.started"), {})


def wanted(events: list[dict]) -> bool:
    """Runs worth rendering: Droid wrote code and the sim ran it on a cup scene from the camera."""
    return any(e["type"] == "skill.diff" for e in events) and any(
        e["type"] == "attempt.started" and (e.get("data") or {}).get("env") == "sim" for e in events)


def render(cfg, source: str, scene_dict: dict, task: dict, version: int, mp4: Path, jpg: Path) -> dict:
    import imageio
    import mujoco
    import numpy as np

    from skillsmith.events import NullBus
    from skillsmith.hal import sim
    from skillsmith.skills.runtime import run_skill
    from skillsmith.types import Scene

    scene = Scene.from_dict(scene_dict)
    env = sim.make_sim_env(cfg, scene)
    arm = env.arm
    renderer = mujoco.Renderer(arm.model, *SIZE)
    target = scene.objects[0] if scene.objects else None
    cams = []
    for az in (135, 200):
        c = mujoco.MjvCamera()
        c.lookat[:] = [target.x, target.y, 0.05] if target else [0.2, 0.0, 0.05]
        c.distance, c.elevation, c.azimuth = 0.62, -28, az
        cams.append(c)
    frames, n = [], [0]
    orig = arm._step

    def shot() -> None:
        views = []
        for c in cams:
            renderer.update_scene(arm.data, camera=c)
            views.append(renderer.render())
        frames.append(np.hstack(views))

    def step() -> None:
        orig()
        n[0] += 1
        if n[0] % STEPS_PER_FRAME == 0:
            shot()

    arm._step = step
    shot()
    try:
        result = run_skill(source, env, task.get("params") or {}, cfg, NullBus(), skill=task.get("skill") or "skill",
                           version=version, success=task.get("success"))
        ok, reason = result.ok, result.reason()
    except Exception as exc:  # a crash in the rebuilt code is still a rendering of what it did
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    finally:
        shot()
        env.close()
    frames.extend([frames[-1]] * FPS)  # hold the end for a second so the loop shows where it stopped
    imageio.mimwrite(mp4, frames, fps=FPS, quality=7, macro_block_size=8)
    imageio.imwrite(jpg, frames[-1], quality=85)
    return {"ok": bool(ok), "reason": reason, "frames": len(frames)}


def render_run(cfg, run_dir: Path, force: bool) -> Optional[dict]:
    events = read_events(run_dir / "events.jsonl")
    if not wanted(events):
        return None
    out_dir = run_dir / "sim"
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists() and not force:
        print(f"{run_dir.name}: already rendered (--force to redo)")
        return json.loads(manifest_path.read_text())
    task, code = task_of(events), versions(events)
    out_dir.mkdir(exist_ok=True)
    renders, done = [], set()
    for a in sim_attempts(events):
        v = a["version"]
        if v in done or v not in code or a["scene"] is None:
            continue
        done.add(v)
        mp4, jpg = out_dir / f"v{v}.mp4", out_dir / f"v{v}.jpg"
        rerun = render(cfg, code[v], a["scene"], task, v, mp4, jpg)
        (out_dir / f"v{v}.py").write_text(code[v])
        renders.append({"version": v, "video": str(mp4), "poster": str(jpg), "recorded": a["recorded"], "rerun": rerun})
        print(f"{run_dir.name} v{v}: recorded {'PASS' if a['recorded']['ok'] else 'FAIL'}, "
              f"today's sim {'PASS' if rerun['ok'] else 'FAIL'} ({rerun['frames']} frames)")
    manifest = {"run_id": run_dir.name, "skill": task.get("skill"), "renders": renders}
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> int:
    from skillsmith.config import load_config

    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("run_ids", nargs="*")
    p.add_argument("--force", action="store_true", help="re-render runs that already have a manifest")
    p.add_argument("--profile", default=None, help="config overlay(s), e.g. scratch")
    args = p.parse_args()
    cfg = load_config(profile=args.profile)
    runs_dir = Path(cfg.path(cfg.repair.runs_dir))
    dirs = [runs_dir / r for r in args.run_ids] or sorted(d for d in runs_dir.iterdir() if (d / "events.jsonl").is_file())
    for d in dirs:
        try:
            render_run(cfg, d, args.force)
        except Exception as exc:
            print(f"{d.name}: could not render: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
