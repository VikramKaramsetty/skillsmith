"""Reset the real arm: open the gripper (clears a squeezing/overloaded gripper), then go home slowly.

    uv run -q --extra dev --extra dashboard --extra kin --extra vision --extra sim --extra droid python tools/reset_arm.py

Talks to robotd only (robot.robotd_url); robotd must be running. Watch the arm: it moves.
Home = robot.home_xyz with the gripper pointing down, at --speed deg/s (default 15).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from skillsmith.config import load_config
from skillsmith.kinematics import make_kinematics


def _call(url: str, path: str, body: dict | None = None, timeout: float = 60.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--speed", type=float, default=15.0, help="joint speed, deg/s")
    args = ap.parse_args()
    cfg = load_config()
    url = cfg.robot.robotd_url
    try:
        _call(url, "/gripper", {"pct": float(cfg.robot.gripper_open)})
    except urllib.error.HTTPError as e:  # an overloaded gripper can 500 here and still open
        print(f"gripper open answered {e.code}; checking state", file=sys.stderr)
    joints = _call(url, "/state")["joints"]
    kin = make_kinematics(cfg)
    x, y, z = kin.fk(joints)
    clear_z = max(z, float(cfg.robot.home_xyz[2]))  # first straight up, clear of anything on the table
    level = kin.tool_axis(joints)[2] > -0.5  # keep a level gripper level while it lifts (it may hold a cup)
    for k in range(1, 4):
        p = (x, y, z + (clear_z - z) * k / 3)
        up = None if clear_z <= z + 0.005 else (kin.ik(p, joints, approach="side") if level else kin.ik(p, joints))
        if up is not None:
            _call(url, "/move_joints", {"target": up, "speed_dps": args.speed})
            joints = up
    home = kin.ik(tuple(cfg.robot.home_xyz), [*joints[:4], 0.0, joints[5]])
    if home is None:
        print(f"home {cfg.robot.home_xyz} unreachable", file=sys.stderr)
        return 1
    _call(url, "/move_joints", {"target": home, "speed_dps": args.speed})
    state = _call(url, "/state")
    print("home:", [round(v, 1) for v in state["joints"]], "gripper", round(state["gripper_pos"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
