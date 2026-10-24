"""HSV colour-blob detector + table homography. OWNER: WS3.

Contract:
- detect(frame_bgr, homography, known_objects) -> Scene
  * HSV thresholds per colour from config (tune live with a slider tool).
  * Blob centroid (bottom-centre for cups seen at an angle) → table (x, y) via the
    3x3 homography from config/homography.json.
  * z: 0 unless the blob sits on another object's footprint (stacked) → z = that top.
  * height/width: from a known-object table (e.g. {"cup": (0.08, 0.065)}), not measured.
- calibrate(frame, marker_ids_to_xy) -> homography: ArUco markers at known robot-frame
  table points; write config/homography.json.

Imports cv2 lazily: core tests must run without OpenCV.
"""

from __future__ import annotations


def detect(frame_bgr, homography, known_objects):
    raise NotImplementedError("WS3")
