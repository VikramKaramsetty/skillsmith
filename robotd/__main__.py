"""Entry point: `python -m robotd [--mock] [--port 8100] [--host 127.0.0.1] [--config PATH]`. OWNER: WS1.

robotd is standalone: it runs in its own LeRobot venv (~/robot/.venv), so it imports nothing from skillsmith and
reads config/robot.yaml itself, plus config/<profile>.yaml overlays (--profile or $SKILLSMITH_PROFILE,
comma-separated, same merge as skillsmith.config: `--profile mac` runs the arm on a Mac).
It binds 127.0.0.1 by default; any other host needs a shared token in $ROBOTD_TOKEN, which clients
send as the X-Robotd-Token header (the Mac fallback link).
Without --mock it opens the follower (config robot.follower_port / robot.calibration_id) through
LeRobot, holding its current pose; on Ctrl-C it disconnects and the arm goes limp (support it).
Cameras come from config `cameras:` (a V4L path or an MJPEG URL each; robotd/cameras.py).
`--cameras-only` runs the real cameras with a mock arm, for a camera check without the serial port.
Agents only ever run it with --mock.
"""

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from robotd import cameras, feetech
from robotd.controller import ArmController, Bus
from robotd.mock import MockBus, MockCamera
from robotd.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "robot.yaml"
CAMERAS = ("front", "wrist")
TOKEN_ENV = "ROBOTD_TOKEN"

Serve = Callable[[object, str, int], None]


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m robotd", description="Owns the arm's serial port and cameras.")
    p.add_argument("--mock", action="store_true", help="fake arm and cameras (no hardware)")
    p.add_argument("--cameras-only", action="store_true",
                   help="real cameras, mock arm: check the cameras without opening the serial port")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--profile", default=os.environ.get("SKILLSMITH_PROFILE"),
                   help="config overlays merged on top, e.g. mac or mac,stage (config/<name>.yaml)")
    return p.parse_args(argv)


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge `over` onto `base`: the same rule as skillsmith.config._merge."""
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_config(path: str, profile: Optional[str] = None) -> dict:
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)
    for name in [n.strip() for n in (profile or "").split(",") if n.strip()]:
        overlay = Path(path).parent / f"{name}.yaml"
        if not overlay.exists():
            raise FileNotFoundError(f"no config profile '{name}' ({overlay})")
        with open(overlay) as f:
            data = _merge(data, yaml.safe_load(f) or {})
    return data


def build_controller(cfg: dict, bus: Bus) -> ArmController:
    safety = cfg["safety"]
    return ArmController(bus, max_step_deg=float(safety["max_step_deg"]),
                         max_speed_dps=float(safety["max_joint_speed_dps"]),
                         limit_margin_deg=float(safety.get("joint_limit_margin_deg", 2.0)),
                         max_lag_deg=float(safety.get("max_lag_deg", safety["max_step_deg"])))


def open_bus(mock: bool, cfg: dict) -> Bus:
    if mock:
        return MockBus()
    robot = cfg["robot"]
    return feetech.connect_follower(robot["follower_port"], robot["calibration_id"])


def open_cameras(mock: bool, cfg: dict) -> dict:
    if mock:
        return {name: MockCamera(name) for name in CAMERAS}
    spec = cfg["cameras"]
    sizes = {name: tuple(spec[f"{name}_size"]) if spec.get(f"{name}_size") else None for name in CAMERAS}
    return {name: cameras.CaptureCamera(name, lambda s=spec[name], z=sizes[name]: cameras.open_capture(s, z))
            for name in CAMERAS if spec.get(name)}


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _uvicorn(app, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)


def main(argv: Optional[Sequence[str]] = None, serve: Optional[Serve] = None) -> int:
    args = parse_args(argv)
    token = os.environ.get(TOKEN_ENV) or None
    if not is_loopback(args.host) and not token:
        print(f"robotd: refusing to listen on {args.host} without a token: set {TOKEN_ENV} "
              "(clients send it as the X-Robotd-Token header)", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="robotd %(levelname)s %(message)s")
    cfg = load_config(args.config, args.profile)
    try:
        bus = open_bus(args.mock or args.cameras_only, cfg)
    except Exception as e:  # LeRobot raises its own types; the operator needs the reason, not a traceback
        print(f"robotd: cannot open the arm: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    cams = open_cameras(args.mock, cfg)
    app = create_app(build_controller(cfg, bus), cams, token=token)
    try:
        (serve or _uvicorn)(app, args.host, args.port)
    finally:
        for cam in cams.values():
            getattr(cam, "close", lambda: None)()
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
