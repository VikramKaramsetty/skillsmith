"""MuJoCo sim backend. OWNER: WS2 (docs/workstreams/WS2_kinematics_sim.md).

Contract (docs/INTERFACES.md §2):
- make_sim_env(cfg, scene) -> Env with arm/kin/perception backed by one MuJoCo model.
- The world is built FROM the latest real Scene: each SceneObject becomes a body
  (cylinder for cups, box for blocks) at its x, y, z with its height and width.
- arm.move_joints steps physics until the joints settle (or times out → raise).
- Unintended contacts (arm/held object vs table or other objects, excluding the
  gripper pads against the grasped object) raise CollisionError AND are exposed via
  arm.safety_issues() -> list[SafetyIssue].
- arm.last_video_path: an mp4 of the attempt rendered offscreen (for the dashboard).
- perception.observe() returns ground-truth object poses from the sim.
- Kinematics should be the SAME implementation used on the real arm (placo via
  LeRobot RobotKinematics on so101_new_calib.urdf) so sim validates the real IK.
"""

from __future__ import annotations

from typing import Optional

from skillsmith.types import Scene


def make_sim_env(cfg, scene: Optional[Scene] = None):
    raise NotImplementedError(
        "sim backend not built yet (WS2). Use --env fake, or see docs/workstreams/WS2_kinematics_sim.md")
