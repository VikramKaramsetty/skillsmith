"""Save the EMPTY table as the reference image for the model-free cup detector (detect.backend: diff).

    uv run --extra vision python tools/capture_background.py [--out runs/background.png] [--frames 15]

Clear the mat first (no cups, hands or tools; park the arm where it rests). Takes the per-pixel median of
several frames from robotd's front camera, so one flicker or passer-by does not end up in it.
Redo it after the lighting changes or the camera moves.
"""

import argparse
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv=None) -> int:
    import cv2
    import numpy as np

    from skillsmith.config import load_config

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="runs/background.png")
    p.add_argument("--frames", type=int, default=15)
    args = p.parse_args(argv)
    url = load_config().robot.robotd_url.rstrip("/") + "/frame/front"
    frames = []
    for _ in range(args.frames):
        data = urllib.request.urlopen(url, timeout=5).read()
        frames.append(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR))
        time.sleep(0.2)
    median = np.median(np.stack(frames), axis=0).astype(np.uint8)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(args.out, median)
    print(f"saved {args.out} ({median.shape[1]}x{median.shape[0]}, median of {len(frames)} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
