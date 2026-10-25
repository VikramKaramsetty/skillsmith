"""Printable calibration and table-layout sheets for the demo table (all at 1:1 scale).

    uv run --no-project --with reportlab --with opencv-python-headless --with numpy \
        python print/make_printouts.py

Writes into print/:
- checkerboard_9x6_25mm.pdf   intrinsics board: 10x7 squares of 25 mm (= 9x6 inner corners)
- aruco_markers_40mm.pdf      DICT_4X4_50 ids 0-5, 40 mm black square, 60 mm cut line
- table_mat_tiled.pdf         the table mat split over US Letter pages to trim and tape together
- table_mat_full.pdf          the same mat on one 24x18 in landscape page (print shop / plotter)

Numbers match tools/calibrate_camera.py defaults (--pattern 9x6 --square 0.025 --marker-size 0.04)
and config/robot.yaml (workspace, reach). Robot frame: x forward, y left, origin under shoulder_pan.
"""

import math
from pathlib import Path

import cv2
from reportlab.lib.colors import Color, black, white
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.units import inch, mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent
GREY = Color(0.62, 0.62, 0.62)
LIGHT = Color(0.85, 0.85, 0.85)

PATTERN = (9, 6)            # inner corners
SQUARE_MM = 25.0
MARKER_MM = 40.0            # black square edge
MARKER_CUT_MM = 60.0        # white quiet zone included
MARKER_IDS = [0, 1, 2, 3, 4, 5]

# Table mat in the robot frame (metres).
MAT_X = (-0.07, 0.37)       # behind the base .. past max reach
MAT_Y = (-0.24, 0.24)
WORKSPACE_X = (0.08, 0.34)
WORKSPACE_Y = (-0.20, 0.20)
MIN_REACH, MAX_REACH = 0.08, 0.33
MAT_MARKERS = {0: (0.10, -0.18), 1: (0.29, -0.14), 2: (0.29, 0.14), 3: (0.10, 0.18)}
ACCURACY_MARKS = {1: (0.22, 0.0), 2: (0.15, -0.10), 3: (0.15, 0.10), 4: (0.28, -0.10), 5: (0.28, 0.10)}


def scale_bar(c, x, y, length_mm=100):
    """A 100 mm bar to check the printer did not scale the page."""
    c.setLineWidth(0.6)
    c.line(x, y, x + length_mm * mm, y)
    for i in range(0, length_mm + 1, 10):
        h = 3 * mm if i % 50 == 0 else 1.6 * mm
        c.line(x + i * mm, y, x + i * mm, y + h)
    c.setFont("Helvetica", 7)
    c.drawString(x, y - 3.2 * mm, f"{length_mm} mm: measure this. If it is not exactly {length_mm} mm, "
                 "reprint at 100% / Actual size.")


def checkerboard():
    page_w, page_h = landscape(letter)
    c = canvas.Canvas(str(OUT / "checkerboard_9x6_25mm.pdf"), pagesize=(page_w, page_h))
    cols, rows = PATTERN[0] + 1, PATTERN[1] + 1
    s = SQUARE_MM * mm
    x0 = (page_w - cols * s) / 2
    y0 = (page_h - rows * s) / 2 + 3 * mm
    for r in range(rows):
        for q in range(cols):
            if (r + q) % 2 == 0:
                c.rect(x0 + q * s, y0 + r * s, s, s, stroke=0, fill=1)
    c.setFont("Helvetica", 7)
    c.setFillColor(GREY)
    c.drawString(x0, y0 - 5 * mm, f"Skillsmith checkerboard: {cols}x{rows} squares = {PATTERN[0]}x{PATTERN[1]} inner "
                 f"corners, {SQUARE_MM:.0f} mm squares. Edge to edge = {cols * SQUARE_MM:.0f} x {rows * SQUARE_MM:.0f} "
                 "mm. Measure; if different, pass --square <metres>.")
    c.showPage()
    c.save()


def marker_modules(marker_id):
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    return cv2.aruco.generateImageMarker(dictionary, marker_id, 6)  # 6x6 modules incl. the black border


def draw_marker(c, cx, cy, marker_id):
    """Marker centred at (cx, cy) in points, printed top edge up the page, inside a 60 mm cut square."""
    img = marker_modules(marker_id)
    n = img.shape[0]
    m = MARKER_MM * mm / n
    left, top = cx - MARKER_MM * mm / 2, cy + MARKER_MM * mm / 2
    half = MARKER_CUT_MM * mm / 2
    c.setFillColor(black)
    for r in range(n):
        for q in range(n):
            if img[r, q] < 128:
                c.rect(left + q * m, top - (r + 1) * m, m * 1.002, m * 1.002, stroke=0, fill=1)
    c.setDash(2, 2)
    c.setStrokeColor(GREY)
    c.setLineWidth(0.4)
    c.rect(cx - half, cy - half, 2 * half, 2 * half, stroke=1, fill=0)
    c.setDash()
    c.setStrokeColor(black)
    # the label sits inside the quiet zone near the top edge, so it survives the cut
    c.setFont("Helvetica-Bold", 6.5)
    c.drawCentredString(cx, cy + MARKER_MM * mm / 2 + 3 * mm, f"ID {marker_id} · TOP EDGE")


def aruco_sheet():
    page_w, page_h = letter
    c = canvas.Canvas(str(OUT / "aruco_markers_40mm.pdf"), pagesize=letter)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(15 * mm, page_h - 15 * mm, "ArUco markers · DICT_4X4_50 · black square 40 mm · cut on the dashed 60 mm line")
    c.setFont("Helvetica", 8)
    c.drawString(15 * mm, page_h - 20 * mm, "Glue onto stiff matte card, keep the white border. IDs 0-3 go on the table mat; "
                 "4-5 are spares. Small label = the marker's TOP edge.")
    xs = [page_w / 2 - 50 * mm, page_w / 2 + 50 * mm]
    ys = [page_h - 62 * mm, page_h - 142 * mm, page_h - 222 * mm]
    for i, marker_id in enumerate(MARKER_IDS):
        draw_marker(c, xs[i % 2], ys[i // 2], marker_id)
    scale_bar(c, 15 * mm, 14 * mm)
    c.showPage()
    c.save()


# ---- table mat --------------------------------------------------------------------------------

def mat_size_pts():
    return (MAT_Y[1] - MAT_Y[0]) * 1000 * mm, (MAT_X[1] - MAT_X[0]) * 1000 * mm


def to_mat(x, y):
    """Robot (x, y) metres -> mat points from the mat's bottom-left. +x runs up the page, +y to the left."""
    return (MAT_Y[1] - y) * 1000 * mm, (x - MAT_X[0]) * 1000 * mm


def draw_grid(c, w, h):
    c.setLineWidth(0.4)
    c.setFont("Helvetica", 6)
    for i in range(int(math.ceil(MAT_X[0] * 20)), int(MAT_X[1] * 20) + 1):
        _, v = to_mat(i / 20, 0)
        c.setStrokeColor(GREY if i == 0 else LIGHT)
        c.line(0, v, w, v)
        c.setFillColor(GREY)
        c.drawString(2 * mm, v + 1 * mm, f"x={i * 5}cm")
    for j in range(int(math.ceil(MAT_Y[0] * 20)), int(MAT_Y[1] * 20) + 1):
        u, _ = to_mat(0, j / 20)
        c.setStrokeColor(GREY if j == 0 else LIGHT)
        c.line(u, 0, u, h)
        c.setFillColor(GREY)
        c.drawString(u + 1 * mm, 2 * mm, f"y={j * 5}")
    c.setFillColor(black)


def draw_workspace(c):
    c.setStrokeColor(Color(0.35, 0.35, 0.35))
    c.setLineWidth(1.0)
    c.setDash(4, 3)
    u0, v0 = to_mat(WORKSPACE_X[0], WORKSPACE_Y[1])
    u1, v1 = to_mat(WORKSPACE_X[1], WORKSPACE_Y[0])
    c.rect(u0, v0, u1 - u0, v1 - v0, stroke=1, fill=0)
    ou, ov = to_mat(0, 0)
    for r in (MIN_REACH, MAX_REACH):
        c.circle(ou, ov, r * 1000 * mm, stroke=1, fill=0)
    c.setDash()
    c.setFont("Helvetica", 7)
    lu, lv = to_mat(WORKSPACE_X[1], WORKSPACE_Y[1])
    c.drawString(lu + 1.5 * mm, lv - 4 * mm, "workspace box (config)")
    au, av = to_mat(MAX_REACH * math.cos(math.radians(40)), MAX_REACH * math.sin(math.radians(40)))
    c.drawString(au + 1 * mm, av + 1 * mm, f"max reach {MAX_REACH * 100:.0f} cm")
    c.setStrokeColor(black)


def draw_origin(c):
    ou, ov = to_mat(0, 0)
    c.setStrokeColor(black)
    c.setLineWidth(1.2)
    c.line(ou - 12 * mm, ov, ou + 12 * mm, ov)
    c.line(ou, ov - 12 * mm, ou, ov + 12 * mm)
    c.circle(ou, ov, 4 * mm, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(ou + 6 * mm, ov - 9 * mm, "ORIGIN: shoulder_pan axis straight above this cross")
    c.setFont("Helvetica", 7)
    c.drawString(ou + 6 * mm, ov - 12.5 * mm, "robot base sits here, facing up the page (+x)")
    c.setLineWidth(1.0)
    tu, tv = to_mat(0.05, 0)
    c.line(ou, ov, tu, tv)
    c.line(tu, tv, tu - 2 * mm, tv - 3 * mm)
    c.line(tu, tv, tu + 2 * mm, tv - 3 * mm)
    c.drawString(tu + 2 * mm, tv - 1 * mm, "+x")
    yu, yv = to_mat(0, 0.05)
    c.line(ou, ov, yu, yv)
    c.line(yu, yv, yu + 3 * mm, yv + 2 * mm)
    c.line(yu, yv, yu + 3 * mm, yv - 2 * mm)
    c.drawString(yu - 5 * mm, yv + 2 * mm, "+y")


def draw_marker_spots(c):
    half = MARKER_CUT_MM * mm / 2
    for marker_id, (x, y) in MAT_MARKERS.items():
        u, v = to_mat(x, y)
        c.setFillColor(white)
        c.rect(u - half, v - half, 2 * half, 2 * half, stroke=0, fill=1)  # keep the grid out of the quiet zone
        c.setFillColor(black)
        c.setLineWidth(0.6)
        c.rect(u - half, v - half, 2 * half, 2 * half, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(u, v + 4 * mm, f"MARKER {marker_id}")
        c.setFont("Helvetica", 6.5)
        c.drawCentredString(u, v - 1 * mm, "stick marker here,")
        c.drawCentredString(u, v - 4 * mm, "its TOP edge toward +x")
        c.drawCentredString(u, v - half - 3.5 * mm, f"nominal ({x * 100:.0f}, {y * 100:.0f}) cm")


def draw_targets(c):
    for k, (x, y) in ACCURACY_MARKS.items():
        u, v = to_mat(x, y)
        c.setLineWidth(0.9)
        c.line(u - 5 * mm, v, u + 5 * mm, v)
        c.line(u, v - 5 * mm, u, v + 5 * mm)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(u + 1.5 * mm, v + 1.5 * mm, f"T{k}")
        c.setFont("Helvetica", 5.5)
        c.drawString(u + 1.5 * mm, v - 3.5 * mm, f"({x * 100:.0f},{y * 100:.0f})")
    c.setStrokeColor(black)


def draw_mat(c):
    w, h = mat_size_pts()
    c.setLineWidth(0.5)
    c.rect(0, 0, w, h, stroke=1, fill=0)
    c.saveState()
    edge = c.beginPath()
    edge.rect(0, 0, w, h)
    c.clipPath(edge, stroke=0, fill=0)  # the reach circles run past the mat edge
    draw_grid(c, w, h)
    draw_workspace(c)
    draw_origin(c)
    draw_marker_spots(c)
    draw_targets(c)
    c.restoreState()
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(w - 4 * mm, 16 * mm, "Skillsmith table mat · 1:1 · robot frame (cm)")
    c.setFont("Helvetica", 6.5)
    c.drawRightString(w - 4 * mm, 12 * mm, "T1-T5: accuracy marks (WS3.9) · cups can go anywhere inside the workspace")
    c.drawRightString(w - 4 * mm, 8.5 * mm, "Grid squares are 5 cm: measure one to check the print scale")


def table_mat_full():
    page_w, page_h = 24 * inch, 18 * inch     # landscape: the mat is 48 cm wide, 44 cm deep
    w, h = mat_size_pts()
    c = canvas.Canvas(str(OUT / "table_mat_full.pdf"), pagesize=(page_w, page_h))
    c.translate((page_w - w) / 2, (page_h - h) / 2)
    draw_mat(c)
    c.showPage()
    c.save()


def tile_name(row_from_top, col):
    return f"{chr(65 + row_from_top)}{col + 1}"


def tiling_cover(c, page_w, page_h, ncols, nrows):
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, page_h - 25 * mm, "Table mat: how the sheets go together")
    c.setFont("Helvetica", 9)
    lines = [
        f"{ncols * nrows} sheets, {ncols} across x {nrows} down. Print at 100% / Actual size (never 'fit to page').",
        "Each sheet has a 12 mm white margin around a grey border line with a cross at each corner.",
        "Except on the left column, cut off the LEFT margin exactly on the border line.",
        "Except on the bottom row, cut off the BOTTOM margin exactly on the border line.",
        "Lay each trimmed edge on its neighbour's border line so the corner crosses meet; tape on the back.",
        "The bottom row is the robot end (ROBOT END). +x (forward) points up the page.",
    ]
    for i, t in enumerate(lines):
        c.drawString(20 * mm, page_h - 35 * mm - i * 5 * mm, t)
    s = 25 * mm
    gx, gy = 30 * mm, page_h - 90 * mm - nrows * s
    for r in range(nrows):
        for q in range(ncols):
            c.rect(gx + q * s, gy + r * s, s, s)
            c.drawCentredString(gx + q * s + s / 2, gy + r * s + s / 2 - 3, tile_name(nrows - 1 - r, q))
    c.drawString(gx, gy - 6 * mm, "ROBOT END (this edge goes at the robot base)")
    scale_bar(c, 20 * mm, 20 * mm)
    c.showPage()


def table_mat_tiled():
    """Split the mat over portrait Letter pages; each page shows its region inside a 12 mm margin."""
    page_w, page_h = letter
    margin = 12 * mm
    tile_w, tile_h = page_w - 2 * margin, page_h - 2 * margin
    w, h = mat_size_pts()
    ncols, nrows = math.ceil(w / tile_w), math.ceil(h / tile_h)
    c = canvas.Canvas(str(OUT / "table_mat_tiled.pdf"), pagesize=letter)
    tiling_cover(c, page_w, page_h, ncols, nrows)
    for r in range(nrows - 1, -1, -1):          # top row first: A1, A2, ...
        for q in range(ncols):
            c.saveState()
            path = c.beginPath()
            path.rect(margin, margin, tile_w, tile_h)
            c.clipPath(path, stroke=0, fill=0)
            c.translate(margin - q * tile_w, margin - r * tile_h)
            draw_mat(c)
            c.restoreState()
            c.setLineWidth(0.5)
            c.setStrokeColor(GREY)
            c.rect(margin, margin, tile_w, tile_h, stroke=1, fill=0)
            c.setStrokeColor(black)
            for cx in (margin, margin + tile_w):
                for cy in (margin, margin + tile_h):
                    c.line(cx - 4 * mm, cy, cx + 4 * mm, cy)
                    c.line(cx, cy - 4 * mm, cx, cy + 4 * mm)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(margin + 6 * mm, page_h - margin + 3 * mm,
                         f"Sheet {tile_name(nrows - 1 - r, q)}  (row {nrows - r} of {nrows}, column {q + 1} of {ncols})")
            notes = [n for n, keep in (("cut LEFT margin", q > 0), ("cut BOTTOM margin", r > 0)) if keep]
            c.setFont("Helvetica", 7)
            c.drawRightString(page_w - margin - 6 * mm, page_h - margin + 3 * mm, ", ".join(notes) or "keep all margins")
            if r == 0:
                c.drawString(margin, 4 * mm, "ROBOT END")
            c.showPage()
    c.save()
    return ncols, nrows


def main():
    checkerboard()
    aruco_sheet()
    table_mat_full()
    print("tiles (cols, rows):", table_mat_tiled())
    print("wrote", sorted(p.name for p in OUT.glob("*.pdf")))


if __name__ == "__main__":
    main()
