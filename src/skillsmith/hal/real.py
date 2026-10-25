"""Real-arm backend. OWNERS: WS1 (robotd client + arm) and WS3 (perception).

Contract (docs/INTERFACES.md §2):
- RobotdArm(ArmBackend): thin HTTP client of robotd (cfg.robot.robotd_url). It never
  opens the serial port itself.
- Kinematics: the shared placo/RobotKinematics wrapper from WS2 (skillsmith/kinematics.py).
- Perception: WS3's WorldModel (perception/world.py): live tracking, ray-cast positions, bump detection.

RobotdArm (WS1.5):
- `RobotdArm(base_url, token=None, timeout_s=5.0, move_timeout_s=120.0, bus=None)`. Plain urllib
  (core deps only), never through an HTTP proxy from the environment. Sends `X-Robotd-Token` when a
  token is given; `make_real_env` reads it from $ROBOTD_TOKEN.
- Every non-2xx answer, unreachable robotd or malformed reply raises `RobotdError` (status, the
  server's error and details) AND emits an `error` event (`where: "robotd.<call>"`) when a bus is
  attached. The bus is optional because `make_env` takes none; set `arm.bus` to attach it later.
- `move_joints` blocks for as long as robotd does (up to move_timeout_s). If the HTTP call times out
  or the link drops mid-move, the arm may still be moving, so the client sends /stop before raising.
- robotd enforces joint limits, per-step deltas and the watchdog; this client adds no motion logic.

make_real_env(cfg, bus=None) (WS1.5):
- RobotdArm at cfg.robot.robotd_url, checked with one GET /state so a dead robotd fails at startup
  with the command to start it, not mid-skill.
- PlacoKinematics via `make_kinematics(cfg)` (the same IK the sim gate validates).
- WorldModel(cfg, frame_source=RobotdFrames(front), kinematics, arm, bus=bus): frames only via robotd.
  Its live loop is started here (observe() returns the loop's latest Scene and bumps are only seen by
  the loop); Env.close() stops it. No camera calibration (config/camera.json) fails here with the
  command that writes it.
- Anything that fails after the arm is built closes what was built, then re-raises.

HUMAN-ONLY: nothing in this module is run by agents against a real robotd. See AGENTS.md "Hard rules".
Tests drive it against robotd's mock backend.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, List, Optional, Sequence

from skillsmith.events import EventBus
from skillsmith.perception.frames import TOKEN_ENV, TOKEN_HEADER

N_JOINTS = 6
FRONT_CAM = "front"
HOLD_WIDE_GAP = 4.0  # gripper %; jaws this far short of a commanded close hold something (empty closes to ~1.6)
START_HINT = "a human starts it: ~/robot/.venv/bin/python -m robotd --port 8100 (docs/RUNBOOK.md)"

# robotd is on localhost or a link-local cable: never route it through an HTTP proxy from the environment
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RobotdError(RuntimeError):
    """robotd refused or failed a command (`status` = HTTP status), or was unreachable (status 0)."""

    def __init__(self, call: str, status: int, message: str, details: Optional[dict] = None):
        super().__init__(f"robotd {call}: {message}" if status == 0 else f"robotd {call} -> {status}: {message}")
        self.call = call
        self.status = status
        self.error = message
        self.details = details or {}


class RobotdArm:
    """ArmBackend over robotd's HTTP API. Joints in degrees, gripper 0..100 last."""

    name = "real"

    def __init__(self, base_url: str, token: Optional[str] = None, timeout_s: float = 5.0,
                 move_timeout_s: float = 120.0, bus: Optional[EventBus] = None):
        self.url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        self.move_timeout_s = move_timeout_s
        self.bus = bus

    # ---- ArmBackend ---------------------------------------------------------------------------
    def joints(self) -> List[float]:
        joints = self._field(self.state(), "joints", "GET /state")
        if not isinstance(joints, list) or len(joints) != N_JOINTS:
            raise self._fail(RobotdError("GET /state", 0, f"expected {N_JOINTS} joints, got {joints!r:.120}"))
        return [float(v) for v in joints]

    def move_joints(self, target: Sequence[float], speed_dps: float) -> None:
        body = {"target": [float(v) for v in target], "speed_dps": float(speed_dps)}
        try:
            self._call("POST", "/move_joints", body, self.move_timeout_s)
        except RobotdError as e:
            if e.status == 0:  # timed out or the link dropped: the arm may still be moving
                self._try_stop()
            raise

    def set_gripper(self, pct: float) -> None:
        self._call("POST", "/gripper", {"pct": float(pct)}, self.move_timeout_s)
        self._gripper_goal = float(pct)

    def gripper_holding(self) -> bool:
        """robotd's load-based signal, OR the jaws stopped well short of a commanded close: empty jaws
        close to ~0-2 %, so >= HOLD_WIDE_GAP short means something is between them. A gently ramped
        close on a paper cup never reached robotd's load threshold, so a real grasp read as a miss."""
        st = self.state()
        if bool(self._field(st, "holding", "GET /state")):
            return True
        goal = getattr(self, "_gripper_goal", None)
        return goal is not None and goal < 50 and float(st.get("gripper_pos", 0.0)) - goal >= HOLD_WIDE_GAP

    def stop(self) -> None:
        self._call("POST", "/stop", {})

    # ---- extras (operators, the world model) --------------------------------------------------
    def state(self) -> dict:
        """robotd's /state: joints, holding, gripper_pos, gripper_load, torque, t."""
        return self._call("GET", "/state")

    def set_torque(self, on: bool) -> bool:
        """Torque off makes the arm go limp: support it first."""
        return bool(self._field(self._call("POST", "/torque", {"on": bool(on)}), "torque", "POST /torque"))

    def close(self) -> None:
        pass  # no connection is held between calls

    # ---- HTTP ---------------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: Optional[dict] = None,
              timeout_s: Optional[float] = None) -> dict:
        call = f"{method} {path}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers[TOKEN_HEADER] = self.token
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(request, timeout=timeout_s or self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raise self._fail(_refusal(call, e)) from e
        except (urllib.error.URLError, OSError) as e:  # refused, reset, timed out
            reason = getattr(e, "reason", e)
            raise self._fail(RobotdError(call, 0, f"cannot reach robotd at {self.url} ({reason}); {START_HINT}")) from e
        try:
            reply = json.loads(raw.decode())
        except ValueError as e:
            raise self._fail(RobotdError(call, 0, f"reply is not JSON: {raw[:80]!r}")) from e
        if not isinstance(reply, dict):
            raise self._fail(RobotdError(call, 0, f"reply is not a JSON object: {reply!r:.80}"))
        return reply

    def _fail(self, err: RobotdError) -> RobotdError:
        """Every failure reaches the dashboard as an `error` event before it is raised."""
        if self.bus is not None:
            self.bus.emit("error", where=f"robotd.{err.call.split('/', 1)[1]}", message=str(err))
        return err

    def _field(self, reply: dict, key: str, call: str) -> Any:
        if key not in reply:
            raise self._fail(RobotdError(call, 0, f"reply has no '{key}': {reply!r:.120}"))
        return reply[key]

    def _try_stop(self) -> None:
        try:
            self.stop()
        except RobotdError:
            pass  # already emitted as an event; the caller needs the original failure


def _refusal(call: str, e: urllib.error.HTTPError) -> RobotdError:
    """robotd answers {"ok": false, "error": str, ...details}; FastAPI's own 422 has "detail"."""
    try:
        payload = json.loads(e.read().decode())
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return RobotdError(call, e.code, str(e.reason))
    message = payload.get("error") or payload.get("detail") or e.reason
    details = {k: v for k, v in payload.items() if k not in ("ok", "error", "detail")}
    text = f"{message} {json.dumps(details, default=str)}" if details else str(message)
    return RobotdError(call, e.code, text, details)


def make_real_env(cfg, bus: Optional[EventBus] = None):
    """Env("real"): RobotdArm + PlacoKinematics + WorldModel fed by robotd's front camera."""
    from skillsmith.hal.base import Env
    from skillsmith.kinematics import make_kinematics
    from skillsmith.perception.frames import make_frame_source
    from skillsmith.perception.world import WorldModel

    arm = RobotdArm(cfg.robot.robotd_url, token=os.environ.get(TOKEN_ENV) or None, bus=bus)
    built: List[Any] = [arm]
    try:
        arm.joints()  # fail at startup, with the start command, if robotd is not up
        kin = make_kinematics(cfg)
        kin.side_z_offset = float(cfg.robot.get("side_z_offset", 0.0))  # real-arm level-pose height error
        frames = make_frame_source(cfg, FRONT_CAM)
        built.append(frames)
        perception = WorldModel(cfg, frame_source=frames, kinematics=kin, arm=arm, bus=bus)
        built.append(perception)
        perception.start()
    except BaseException:
        for part in reversed(built):
            _close_quietly(part)
        raise
    return Env("real", arm, kin, perception)


def _close_quietly(part: Any) -> None:
    close = getattr(part, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass  # the original failure is the one to report
