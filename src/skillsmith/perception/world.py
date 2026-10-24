"""Live world model. OWNER: WS3 (WS3.5–WS3.7). Implements the Perception protocol for the real arm.

Contract (docs/ARCHITECTURE.md "Perception and world state"):
- Runs a background loop at 10–15 Hz: overhead frame → detector (YOLO-World/YOLO-E, HSV
  backup) → ray–plane localisation (camera calibration) → association to stable object ids.
- `observe() -> Scene`: the LATEST state, not a fresh one-off detection. Objects hidden behind
  the arm keep their last position with `visible=False`.
- A held object follows the gripper (FK tip position from robotd) instead of being re-detected.
- `unexpected_motion() -> str | None`: a message (and clears it) when a NOT-held object moved
  more than `world.bump_threshold` (default 1 cm), e.g. "red cup moved 3.2 cm toward +y while
  not held". The Robot raises WorldChanged on it before the next motion.
- `perception_ok() -> (bool, str)`: the confidence gate (markers still where calibrated,
  detection confidence, object count stable). The classifier uses it.
- `align_offset(obj) -> (dx, dy)`: wrist-camera fine correction in metres, used by robot.align_over.
- `holding_check() -> (bool, str)`: wrist-camera post-grasp check (WS3.7).
- `snapshot(path)` / `keyframe(phase, path)`: save frames for evidence (annotated by annotate.py).
- Reads frames ONLY through robotd (it owns the cameras).
"""

from __future__ import annotations

from typing import Optional, Tuple

from skillsmith.types import Scene, SceneObject


class WorldModel:
    def __init__(self, cfg, frame_source=None, kinematics=None, arm=None):
        raise NotImplementedError("WS3.5: see docs/workstreams/WS3_perception_world.md")

    def observe(self) -> Scene:
        raise NotImplementedError("WS3.5")

    def unexpected_motion(self) -> Optional[str]:
        raise NotImplementedError("WS3.5")

    def perception_ok(self) -> Tuple[bool, str]:
        raise NotImplementedError("WS3.6")

    def align_offset(self, obj: SceneObject) -> Tuple[float, float]:
        raise NotImplementedError("WS3.7")

    def holding_check(self) -> Tuple[bool, str]:
        raise NotImplementedError("WS3.7")

    def snapshot(self, path: str) -> Optional[str]:
        raise NotImplementedError("WS3.5")
