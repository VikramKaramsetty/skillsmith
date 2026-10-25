"""Where is the gripper tip? Reads robotd's joints and prints the tip's robot-frame (x, y, z) from FK.

    uv run --extra kin python tools/tip.py                    # print the tip position (m)
    uv run --extra kin python tools/tip.py --marker 0         # tip is touching ArUco marker 0: save it
    uv run --extra kin python tools/tip.py --watch            # print it twice a second (Ctrl-C to stop)

Used for camera calibration (docs/RUNBOOK.md): with robotd running and torque OFF, hold the gripper
tip on each taped marker's centre and run `--marker <id>`. That writes config/markers.json
({"<id>": [x, y]}), which `tools/calibrate_camera.py --markers config/markers.json` reads.
It only reads robotd's /state; it never moves the arm.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MARKERS = Path("config/markers.json")


def read_joints(url: str) -> List[float]:
    req = urllib.request.Request(url.rstrip("/") + "/state")
    token = os.environ.get("ROBOTD_TOKEN")
    if token:
        req.add_header("X-Robotd-Token", token)
    with urllib.request.urlopen(req, timeout=3) as r:
        return [float(v) for v in json.load(r)["joints"]]


def save_marker(path: Path, marker_id: int, xy: Sequence[float]) -> Dict[str, List[float]]:
    markers = json.loads(path.read_text()) if path.exists() else {}
    markers[str(marker_id)] = [round(float(xy[0]), 4), round(float(xy[1]), 4)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(markers.items(), key=lambda kv: int(kv[0]))), indent=1) + "\n")
    return markers


def main(argv=None) -> int:
    from skillsmith.config import load_config
    from skillsmith.kinematics import PlacoKinematics

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--marker", type=int, help="save the tip's (x, y) as this ArUco marker's centre")
    p.add_argument("--file", default=str(MARKERS), help="markers file (default config/markers.json)")
    p.add_argument("--watch", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config()
    # Raw URDF frame = the fixed jaw's tip, the point you touch markers with (make_kinematics adds the
    # 3.5 cm grasp-point offset toward the moving jaw, which put every marker up to 3.5 cm off).
    kin = PlacoKinematics(str(cfg.path(cfg.robot.urdf)), cfg.robot.ee_frame, cfg.robot.joint_names[:5], tip_offset=0)
    while True:
        try:
            x, y, z = kin.fk(read_joints(cfg.robot.robotd_url))
        except OSError as e:
            print(f"cannot read robotd at {cfg.robot.robotd_url}: {e} (start it: docs/RUNBOOK.md)", file=sys.stderr)
            return 1
        print(f"tip  x {x:+.4f}  y {y:+.4f}  z {z:+.4f}  m")
        if args.marker is not None:
            markers = save_marker(Path(args.file), args.marker, (x, y))
            print(f"saved marker {args.marker} -> {args.file} ({len(markers)} markers: {sorted(map(int, markers))})")
            if abs(z) > 0.02:
                print(f"warning: tip z is {z * 100:.1f} cm; it should touch the table (check joint signs/FK)")
            return 0
        if not args.watch:
            return 0
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
