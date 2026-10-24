"""Calibrate the overhead camera, or check a calibration. OWNER: WS3 (WS3.2). Run by the arm operator.

    # 1. intrinsics + pose: ~15 checkerboard photos in a folder, ArUco markers taped (top edge toward +x),
    #    their centres measured by touching them with the gripper tip, in markers.json {"0": [x, y], ...}
    python tools/calibrate_camera.py --board shots/ --markers markers.json [--frame overhead.jpg]
    # 2. check (after any bump): re-detected marker centres vs measured, per marker, in cm
    python tools/calibrate_camera.py --check [--frame overhead.jpg]

Without --frame the overhead frame is grabbed from robotd (cfg.robot.robotd_url, camera `front`).
Writes/reads config/camera.json (cameras.calibration in config/robot.yaml).
"""

import argparse
import json
import sys
from pathlib import Path

from skillsmith.config import load_config
from skillsmith.perception.frames import make_frame_source
from skillsmith.perception.geometry import CalibrationError, CameraModel, calibrate

CHECK_LIMIT_M = 0.01


def read_bgr(path):
    import cv2

    image = cv2.imread(str(path))
    if image is None:
        raise SystemExit(f"cannot read image {path}")
    return image


def overhead(args, cfg):
    return read_bgr(args.frame) if args.frame else make_frame_source(cfg, "front").read().bgr()


def run_check(model, frame) -> int:
    errors = model.marker_errors(frame)
    for marker_id in sorted(int(k) for k in model.markers):
        e = errors.get(marker_id)
        print(f"marker {marker_id}: " + ("NOT SEEN" if e is None else f"{e * 100:.2f} cm"))
    bad = [e for e in errors.values() if e > CHECK_LIMIT_M]
    ok = errors and not bad and len(errors) == len(model.markers)
    print("OK" if ok else f"FAIL: need every marker within {CHECK_LIMIT_M * 100:.0f} cm (redo the calibration)")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--board", help="folder of checkerboard photos (*.jpg, *.png)")
    p.add_argument("--markers", help='JSON {"<id>": [x, y]} measured marker centres (m, robot frame)')
    p.add_argument("--frame", help="overhead image with the markers (default: grab from robotd)")
    p.add_argument("--pattern", default="9x6", help="inner corners, cols x rows")
    p.add_argument("--square", type=float, default=0.025, help="checkerboard square edge (m)")
    p.add_argument("--marker-size", type=float, default=0.04, help="black square edge (m)")
    p.add_argument("--out", help="camera.json path (default: config cameras.calibration)")
    p.add_argument("--check", action="store_true", help="check the saved calibration instead")
    args = p.parse_args(argv)
    cfg = load_config()
    out = Path(args.out) if args.out else cfg.path(cfg.cameras.calibration)

    if args.check:
        return run_check(CameraModel.load(out), overhead(args, cfg))
    if not (args.board and args.markers):
        p.error("--board and --markers are required to calibrate (or use --check)")
    shots = sorted(f for f in Path(args.board).iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png"))
    cols, rows = (int(n) for n in args.pattern.lower().split("x"))
    markers = {int(k): tuple(v) for k, v in json.loads(Path(args.markers).read_text()).items()}
    frame = overhead(args, cfg)
    try:
        model = calibrate({"checkerboard": [read_bgr(s) for s in shots], "markers": frame}, markers,
                          {"pattern": (cols, rows), "square": args.square}, marker_size=args.marker_size)
    except CalibrationError as e:
        print(f"calibration failed: {e}", file=sys.stderr)
        return 1
    print(f"saved {model.save(out)} (marker reprojection {model.reprojection_px:.2f} px)")
    return run_check(model, frame)


if __name__ == "__main__":
    sys.exit(main())
