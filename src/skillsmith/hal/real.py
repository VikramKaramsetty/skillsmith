"""Real-arm backend. OWNERS: WS1 (robotd client + arm) and WS3 (perception).

Contract (docs/INTERFACES.md §2):
- RobotdArm(ArmBackend): thin HTTP client of robotd (cfg.robot.robotd_url). It never
  opens the serial port itself.
- Kinematics: the shared placo/RobotKinematics wrapper from WS2 (skillsmith/kinematics.py).
- Perception: WS3's CameraPerception (front-camera homography + colour detection).

HUMAN-ONLY: nothing in this module is run by agents. See AGENTS.md "Hard rules".
"""

from __future__ import annotations


def make_real_env(cfg):
    raise NotImplementedError(
        "real backend not built yet (WS1 + WS3). See docs/workstreams/WS1_robotd_real.md")
