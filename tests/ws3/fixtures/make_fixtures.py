"""Render the synthetic overhead fixture frames and truth.json. OWNER: WS3.

    uv run --extra dev --extra vision python tests/ws3/fixtures/make_fixtures.py

Stand-ins until the arm operator records real ones (`python -m skillsmith.perception.frames record`).
A pinhole camera ~0.78 m over the table, tilted a few degrees, sees cups (open top, dark inside) and
a block, plus four ArUco markers at known table points. Everything is exact, so WS3.2-WS3.4 can test
calibration, ray casting (the stacked scene has ~2-4 cm of table-plane parallax) and detection.

truth.json: "camera" {K, dist, R, t, size} with X_cam = R @ X_robot + t (OpenCV's solvePnP convention);
"markers" {id: [x, y]} centres on the table (DICT_4X4_50, black square `marker_size` m, top edge
toward +x); "frames" [{file, t, objects: [SceneObject fields + top_uv (pixel of the top centre)]}].
"""

import json
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent
SIZE = (640, 480)
K = np.array([[700.0, 0.0, 322.5], [0.0, 700.0, 238.0], [0.0, 0.0, 1.0]])
CAMERA_AT = np.array([0.17, 0.03, 0.78])  # robot frame (m)
LOOK_AT = np.array([0.21, 0.0, 0.0])
MARKERS = {0: (0.06, -0.21), 1: (0.36, -0.21), 2: (0.36, 0.21), 3: (0.06, 0.21)}
MARKER_SIZE = 0.04
TABLE_BGR = (150, 155, 160)
COLORS = {"red": (35, 35, 190), "blue": (180, 90, 30), "green": (50, 160, 50)}
CUP = {"kind": "cup", "height": 0.08, "width": 0.065}
BLOCK = {"kind": "block", "height": 0.03, "width": 0.03}


def obj(color, spec, x, y, z=0.0):
    return {"id": f"{color}_{spec['kind']}_0", "label": f"{color} {spec['kind']}", "color": color,
            "kind": spec["kind"], "x": x, "y": y, "z": z, "height": spec["height"], "width": spec["width"]}


SCENES = [
    ("apart.jpg", [obj("blue", CUP, 0.22, -0.08), obj("red", CUP, 0.22, 0.08), obj("green", BLOCK, 0.15, 0.0)]),
    ("stacked.jpg", [obj("red", CUP, 0.22, 0.08), obj("blue", CUP, 0.22, 0.08, z=0.08), obj("green", BLOCK, 0.15, 0.0)]),
    ("edges.jpg", [obj("red", CUP, 0.29, -0.12), obj("blue", CUP, 0.13, 0.13), obj("green", BLOCK, 0.27, 0.06)]),
    # a stack ~17 cm off-centre: ~4 cm of table-plane parallax on the top cup (docs/DECISIONS.md D7)
    ("stacked_far.jpg", [obj("red", CUP, 0.29, -0.09), obj("blue", CUP, 0.29, -0.09, z=0.08),
                         obj("green", BLOCK, 0.15, 0.06)]),
]


def camera_pose():
    z = LOOK_AT - CAMERA_AT
    z /= np.linalg.norm(z)
    down = np.array([-1.0, 0.0, 0.0])  # image down = robot -x, so robot +x points up the image
    y = down - down.dot(z) * z
    y /= np.linalg.norm(y)
    x = np.cross(y, z)
    R = np.stack([x, y, z])
    return R, -R @ CAMERA_AT


R, T = camera_pose()


def project(points):
    cam = (R @ np.asarray(points, float).T).T + T
    uv = (K @ cam.T).T
    return uv[:, :2] / uv[:, 2:3]


def fill(img, points, bgr):
    cv2.fillConvexPoly(img, np.round(points).astype(np.int32), bgr, lineType=cv2.LINE_AA)


def circle(cx, cy, z, r, n=96):
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.stack([cx + r * np.cos(a), cy + r * np.sin(a), np.full(n, z)], axis=1)


def shade(bgr, k):
    return tuple(int(c * k) for c in bgr)


def draw_cup(img, o):
    r, top = o["width"] / 2, o["z"] + o["height"]
    bottom_ring, top_ring = circle(o["x"], o["y"], o["z"], r), circle(o["x"], o["y"], top, r)
    base = COLORS[o["color"]]
    fill(img, cv2.convexHull(np.float32(project(np.vstack([bottom_ring, top_ring])))).reshape(-1, 2), shade(base, 0.7))
    fill(img, project(top_ring), base)  # the rim
    fill(img, project(circle(o["x"], o["y"], top, r * 0.85)), shade(base, 0.45))  # the dark inside


def draw_block(img, o):
    h, top = o["width"] / 2, o["z"] + o["height"]
    square = [(o["x"] + dx, o["y"] + dy) for dx, dy in ((h, h), (h, -h), (-h, -h), (-h, h))]
    corners = [(x, y, z) for z in (o["z"], top) for x, y in square]
    base = COLORS[o["color"]]
    fill(img, cv2.convexHull(np.float32(project(corners))).reshape(-1, 2), shade(base, 0.7))
    fill(img, project([(x, y, top) for x, y in square]), base)


def draw_marker(img, marker_id, cx, cy):
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    black = cv2.aruco.generateImageMarker(dictionary, marker_id, 200)
    tile = cv2.cvtColor(cv2.copyMakeBorder(black, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255), cv2.COLOR_GRAY2BGR)
    h = MARKER_SIZE / 2 * tile.shape[0] / 200  # half-size including the white quiet zone
    world = [(cx + h, cy + h, 0), (cx + h, cy - h, 0), (cx - h, cy - h, 0), (cx - h, cy + h, 0)]  # top edge -> +x
    src = np.float32([[0, 0], [tile.shape[1], 0], [tile.shape[1], tile.shape[0]], [0, tile.shape[0]]])
    H = cv2.getPerspectiveTransform(src, np.float32(project(world)))
    warped = cv2.warpPerspective(tile, H, SIZE)
    mask = cv2.warpPerspective(np.full(tile.shape[:2], 255, np.uint8), H, SIZE)
    img[mask > 127] = warped[mask > 127]


def render(objects, rng):
    img = np.empty((SIZE[1], SIZE[0], 3), np.uint8)
    img[:] = TABLE_BGR
    img = (img * np.linspace(0.92, 1.05, SIZE[1])[:, None, None]).clip(0, 255).astype(np.uint8)  # uneven light
    for marker_id, (x, y) in MARKERS.items():
        draw_marker(img, marker_id, x, y)
    far_first = sorted(objects, key=lambda o: -np.linalg.norm(CAMERA_AT - (o["x"], o["y"], o["z"] + o["height"])))
    for o in far_first:
        (draw_cup if o["kind"] == "cup" else draw_block)(img, o)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    return (img + rng.normal(0, 2.5, img.shape)).clip(0, 255).astype(np.uint8)


def main():
    rng = np.random.default_rng(81)
    frames = []
    for i, (name, objects) in enumerate(SCENES):
        cv2.imwrite(str(OUT / name), render(objects, rng), [cv2.IMWRITE_JPEG_QUALITY, 90])
        for o in objects:
            u, v = project([(o["x"], o["y"], o["z"] + o["height"])])[0]
            o["top_uv"] = [round(float(u), 2), round(float(v), 2)]
        frames.append({"file": name, "t": 1730000000.0 + i, "objects": objects})
    truth = {
        "camera": {"K": K.tolist(), "dist": [0.0] * 5, "R": np.round(R, 9).tolist(), "t": np.round(T, 9).tolist(),
                   "size": list(SIZE), "convention": "X_cam = R @ X_robot + t (OpenCV solvePnP)"},
        "markers": {str(k): list(v) for k, v in MARKERS.items()},
        "marker_size": MARKER_SIZE,
        "marker_dict": "DICT_4X4_50",
        "frames": frames,
    }
    (OUT / "truth.json").write_text(json.dumps(truth, indent=1) + "\n")
    print(f"wrote {len(frames)} frames and truth.json to {OUT}")


if __name__ == "__main__":
    main()
