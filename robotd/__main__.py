"""Entry point: `python -m robotd [--mock] [--port 8100] [--host 127.0.0.1] [--config PATH]`. OWNER: WS1.

robotd is standalone: it runs in the GX10's LeRobot venv, so it imports nothing from skillsmith and
reads config/robot.yaml itself. It binds 127.0.0.1 by default; any other host needs a shared token
in $ROBOTD_TOKEN, which clients send as the X-Robotd-Token header (the Mac fallback link).
Without --mock it opens the follower (config robot.follower_port / robot.calibration_id) through
LeRobot, holding its current pose; on Ctrl-C it disconnects and the arm goes limp (support it).
Agents only ever run it with --mock. Real cameras arrive in WS1.4.
"""

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from robotd import feetech
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
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    return p.parse_args(argv)


def load_config(path: str) -> dict:
    import yaml

    with open(path) as f:
        return yaml.safe_load(f)


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
    cfg = load_config(args.config)
    try:
        bus = open_bus(args.mock, cfg)
    except Exception as e:  # LeRobot raises its own types; the operator needs the reason, not a traceback
        print(f"robotd: cannot open the arm: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    cameras = {name: MockCamera(name) for name in CAMERAS} if args.mock else {}
    app = create_app(build_controller(cfg, bus), cameras, token=token)
    try:
        (serve or _uvicorn)(app, args.host, args.port)
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
