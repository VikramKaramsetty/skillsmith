"""List the cameras OpenCV can open and save one frame from each (HUMAN-ONLY: opens real cameras).

    uv run --extra vision python tools/list_cameras.py          # tries indices 0-5
    open runs/camera-probe/                                     # see which index is which camera

Put the numbers in config/mac.yaml (`cameras.wrist`, `cameras.front`). macOS asks once for camera
access for your terminal app; allow it and run again. Stop robotd first: one process per camera.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(max_index: int = 6) -> int:
    import cv2

    out = Path("runs/camera-probe")
    out.mkdir(parents=True, exist_ok=True)
    found = 0
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        if ok:
            h, w = frame.shape[:2]
            path = out / f"camera-{i}.jpg"
            cv2.imwrite(str(path), frame)
            print(f"index {i}: {w}x{h}  -> {path}")
            found += 1
        cap.release()
    if not found:
        print("no camera opened: check macOS Settings > Privacy & Security > Camera for your terminal app")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 6))
