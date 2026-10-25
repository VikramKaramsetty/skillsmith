# Demo table: what to print and how to put it together

Everything here is 1:1. Print at **100% / Actual size**, never "fit to page", and on **matte**
paper (gloss reflects the lights and breaks marker detection). Every sheet has a 100 mm scale
bar or a 5 cm grid: measure it before you cut anything.

Regenerate the PDFs with:
`uv run --no-project --with reportlab --with opencv-python-headless --with numpy python print/make_printouts.py`

| File | What | Sheets |
|---|---|---|
| `checkerboard_9x6_25mm.pdf` | Camera intrinsics board: 10×7 squares of 25 mm (= 9×6 inner corners) | 1 Letter, landscape |
| `aruco_markers_40mm.pdf` | ArUco DICT_4X4_50, IDs 0–5, 40 mm black square, 60 mm cut line | 1 Letter |
| `table_mat_tiled.pdf` | The table mat, split into 3×2 Letter sheets plus a cover page | 7 Letter |
| `table_mat_full.pdf` | The same mat on one 24×18 in page, for a print shop | 1 (optional) |

These match `tools/calibrate_camera.py` defaults (`--pattern 9x6 --square 0.025 --marker-size 0.04`)
and `config/robot.yaml` (workspace box, reach 8–33 cm).

## Shopping list

- Foam board or stiff card (A4/Letter, 2 pieces), spray adhesive or a glue stick
- Painter's tape (masking tape), a craft knife + steel ruler, a tape measure
- **Overhead USB webcam**, 1080p, ideally with manual focus (e.g. Logitech C920/C922), plugged into the GX10
- **A mount that holds it 60–80 cm above the table, looking straight down**: an overhead phone/camera
  arm with a desk clamp, a C-stand or boom tripod, or a bar across two stands. It must not wobble.
- Cups that don't nest: a small **blue** cup and a **red** cup (upturned), about 6.5 cm across, matte
  if possible. A block as a spare. Check the cup fits the gripper's jaw opening.
- A diffuse light (a desk lamp bounced off a wall works); no hard shadows or glare on the cups

## 1. Make the table mat

1. Print `table_mat_tiled.pdf`. Page 1 is the layout: sheets **A1 A2 A3** (top row) and
   **B1 B2 B3** (bottom row, the robot end).
2. Trim: except on the left column, cut the **left** margin off exactly on the grey border line.
   Except on the bottom row, cut the **bottom** margin off on the border line too.
3. Lay each trimmed edge on its neighbour's border line so the corner crosses meet. Tape the backs.
4. Check: the 5 cm grid squares measure 5 cm across the joins.
5. Place the mat so the **ORIGIN** cross sits exactly under the robot's shoulder_pan (base rotation)
   axis, with **+x** pointing straight out in front of the robot. Tape all four edges down flat.
   Everything else on the mat is drawn in the robot frame from that cross.

What's on the mat:
- **ORIGIN** with the +x / +y arrows (x forward, y left, as in the code)
- The **workspace box** and the 8 cm / 33 cm **reach circles** (dashed): the arm stays inside these
- **MARKER 0–3** boxes: where the ArUco markers go
- **T1–T5** crosses: the accuracy-test marks (WS3.9, issue #89)
- Dashed circles: where the **blue cup** and **red cup** start in the demo

## 2. Make the markers

1. Print `aruco_markers_40mm.pdf`. Check the black squares are 40 mm.
2. Glue the sheet onto foam board, then cut each marker out on its dashed 60 mm line. Keep the white
   border: the detector needs it.
3. Stick markers **0, 1, 2, 3** into the matching boxes on the mat, with the small "ID n · TOP EDGE"
   label toward **+x** (away from the robot). The calibration code assumes that orientation.
4. Keep 4 and 5 as spares. Don't put anything else that looks like a marker in view.

## 3. Make the checkerboard

1. Print `checkerboard_9x6_25mm.pdf`. Measure 10 squares: it should be 250 mm. If not, pass the
   real size to the calibration as `--square <metres per square>`.
2. Glue it flat onto foam board with no bubbles. A bent board ruins the calibration.

## 4. Mount the overhead camera

1. Mount the webcam **60–80 cm above the table**, looking straight down, roughly over the centre
   of the cups (about x = 21 cm, y = 0 on the mat). The whole mat should be in view, all four
   markers clearly visible, and the arm should never hit the camera, even fully raised.
2. Turn off autofocus and autoexposure if the camera allows it (focus changes break the calibration).
3. Tape or zip-tie everything. **Any bump means redoing step 6.**
4. Plug it into the GX10. Only robotd opens it. Point `cameras.front` at it (lead's config change).

## 5. Check the arm first (hardware, human only)

Do issues #72 (robotd smoke test) and #80 (joint signs/offsets, FK vs ruler) first. Use the mat's
5 cm grid as the ruler. The marker positions in step 6 come from FK, so FK must be right first.

## 6. Calibrate the camera (WS3.2)

1. **Checkerboard photos (intrinsics):** with robotd running, hold the board under the overhead
   camera in about 15 poses: tilted different ways, in every part of the frame, near and far.
   Save the frames to a folder, e.g. with
   `python -m skillsmith.perception.frames --cam front --count 20 --every 1.5 --out shots/`
   and move the board between shots.
2. **Measure the markers:** touch the centre of each marker with the gripper tip (the fixed
   jaw's tip) and read the tip position from FK:
   ```bash
   uv run --extra kin python -c "import json,urllib.request; from skillsmith.config import load_config; from skillsmith.kinematics import make_kinematics; cfg=load_config(); j=json.load(urllib.request.urlopen(cfg.robot.robotd_url+'/state'))['joints']; print('tip x=%.3f y=%.3f z=%.3f' % make_kinematics(cfg).fk(j))"
   ```
   Write them into `markers.json`, e.g. `{"0": [0.101, -0.179], "1": [0.291, -0.141], ...}`.
   They should be within about 1 cm of the "nominal" numbers printed on the mat. If they're
   off by more, the mat or FK is wrong: fix that first.
3. Run `python tools/calibrate_camera.py --board shots/ --markers markers.json`. It writes
   `config/camera.json`.
4. Check: `python tools/calibrate_camera.py --check` should print every marker within 1 cm and `OK`.
   Re-run this check before every demo and after any bump.

## 7. Accuracy test (WS3.9, issue #89)

Put a cup on each of **T1–T5** under the demo lighting. The world model must report each one
within 1 cm of the printed coordinates.

## 8. Demo layout

- Blue cup on its dashed circle (right of centre, y = −8 cm); red cup **upside down** on its circle
  (y = +8 cm).
- Nothing else on the mat. Cables and hands stay out of the camera's view.
- Before going on: `calibrate_camera.py --check` says OK, and the dashboard shows both cups.
