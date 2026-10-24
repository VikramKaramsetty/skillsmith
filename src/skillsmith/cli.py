"""`skillsmith` command line.

  skillsmith validate FILE                         static-check a skill
  skillsmith run "stack the blue cup on the red cup" --env fake|sim|real [--author droid|scripted]
  skillsmith demo-fake [--author droid]            full loop, no hardware
  skillsmith serve                                 dashboard + orchestrator (WS6)
"""

from __future__ import annotations

import argparse
import json
import sys

from skillsmith.config import load_config
from skillsmith.events import EventBus, new_run_id
from skillsmith.hal.base import make_env
from skillsmith.skills.library import SkillLibrary
from skillsmith.skills.validator import validate
from skillsmith.tasks import parse_task
from skillsmith.types import TaskSpec


def cmd_validate(args, cfg) -> int:
    issues = validate(open(args.file).read())
    for i in issues:
        print(f"{args.file}:{i}")
    print("OK" if not issues else f"{len(issues)} issue(s)")
    return 1 if issues else 0


def _task(args) -> TaskSpec:
    if args.skill:
        return TaskSpec(args.task, args.skill, json.loads(args.params or "{}"), json.loads(args.success))
    return parse_task(args.task)


def _author(name: str, cfg, bus, drafts=(), repairs=()):
    if name == "droid":
        from skillsmith.repair.droid_client import DroidAuthor
        return DroidAuthor(cfg, bus)
    from skillsmith.repair.scripted import ScriptedAuthor
    return ScriptedAuthor(list(drafts), list(repairs))


def _approver(name: str, env_name: str):
    from skillsmith.repair.approvers import AutoApprover, CliApprover
    if name == "auto":
        return AutoApprover(env_name)
    if name == "dashboard":
        from skillsmith.dashboard.approver import DashboardApprover  # WS6
        return DashboardApprover()
    return CliApprover()


def _run(task: TaskSpec, target, check_env_name, library, author, approver, cfg, bus) -> int:
    from skillsmith.repair.loop import RepairLoop

    def make_check_env(scene):
        return make_env(check_env_name, cfg, scene)

    loop = RepairLoop(cfg, target, make_check_env, library, author, approver, bus)
    try:
        result = loop.run(task)
    finally:
        target.close()
        bus.close()
    print(f"\n{'SUCCESS' if result.ok else 'FAILED'} after {result.rounds} repair round(s); "
          f"events: {bus.run_dir / 'events.jsonl' if bus.run_dir else '-'}")
    return 0 if result.ok else 1


def cmd_run(args, cfg) -> int:
    task = _task(args)
    bus = EventBus(new_run_id(), cfg.path(cfg.repair.runs_dir), echo=True)
    target = make_env(args.env, cfg)
    check = "fake" if args.env == "fake" else "sim"
    library = SkillLibrary(cfg.path(cfg.repair.library_dir))
    return _run(task, target, check, library, _author(args.author, cfg, bus),
                _approver(args.approver, args.env), cfg, bus)


def cmd_demo_fake(args, cfg) -> int:
    from skillsmith.demo import demo_sources, temp_library
    from skillsmith.hal.base import Env
    from skillsmith.hal.fake import FakeWorld

    v1, v2 = demo_sources()
    task = parse_task("stack the blue cup on the red cup")
    bus = EventBus(new_run_id(), cfg.path(cfg.repair.runs_dir), echo=True)
    world = FakeWorld.demo(cfg, rim_slip=True)  # the "real" world has a sim-to-real gap
    target = Env("fake", world, world, world)
    library = SkillLibrary(temp_library())
    author = _author(args.author, cfg, bus, drafts=[v1], repairs=[v2])
    print(f"Task: {task.text}\nLibrary: {library.names()} (temp copy)\n")
    return _run(task, target, "fake", library, author, _approver("auto", "fake"), cfg, bus)


def cmd_serve(args, cfg) -> int:
    try:
        from skillsmith.dashboard.server import main  # WS6
    except ImportError:
        print("dashboard not built yet (WS6: docs/workstreams/WS6_dashboard.md)")
        return 1
    return main(cfg)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="skillsmith")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate")
    v.add_argument("file")

    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--env", choices=["fake", "sim", "real"], default="fake")
    r.add_argument("--author", choices=["droid", "scripted"], default="droid")
    r.add_argument("--approver", choices=["cli", "auto", "dashboard"], default="cli")
    r.add_argument("--skill")
    r.add_argument("--params")
    r.add_argument("--success")

    d = sub.add_parser("demo-fake")
    d.add_argument("--author", choices=["droid", "scripted"], default="scripted")

    sub.add_parser("serve")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    handler = {"validate": cmd_validate, "run": cmd_run, "demo-fake": cmd_demo_fake, "serve": cmd_serve}
    return handler[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
