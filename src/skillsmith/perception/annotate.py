"""Annotated key frames for Droid and the dashboard. OWNER: WS3 (WS3.8).

Contract:
- `annotate_keyframe(frame_bgr, scene, camera, overlays) -> frame_bgr`, where overlays may hold:
  boxes + labels for every object, a crosshair at the planned target, a dot at the gripper tip
  (from FK, projected with the calibrated camera), and an error arrow labelled in cm.
- `crop_pair(before, after, scene, names) -> (img, img)`: tight crops around the objects named
  in the success predicate (for Droid and the optional frontier-vision second opinion).
- Draw with OpenCV, imported lazily. Text big enough to read at 640×480.
"""

from __future__ import annotations


def annotate_keyframe(frame_bgr, scene, camera, overlays):
    raise NotImplementedError("WS3.8: see docs/workstreams/WS3_perception_world.md")


def crop_pair(before_bgr, after_bgr, scene, names):
    raise NotImplementedError("WS3.8")
