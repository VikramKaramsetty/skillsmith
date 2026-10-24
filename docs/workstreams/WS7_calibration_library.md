# WS7: calibration skill + library growth (stretch)

**Branch prefix:** `ws7/` · **Owns:** `src/skillsmith/calibration/`, `skills/library/`, `tests/ws7/`
**Reads:** `droid_template/AGENTS.md`, `droid_template/API.md`, `docs/INTERFACES.md` §3

## Goal
Show the two "extra Droid uses": (1) self-calibration when grasps keep landing off, and
(2) a growing library where new skills reuse old ones (pyramid reuses stack).

## Tasks (one PR each, in order)
- [ ] WS7.1 Calibration routine: test grabs at 4 points, measure xy error with the camera, fit an offset
- [ ] WS7.2 Apply calibration offsets in a wrapper Kinematics without changing the shared contract
- [ ] WS7.3 Library skills put_in, tower and sort_by_color that pass the validator and run on the fake env
- [ ] WS7.4 Pyramid demo task that reuses stack and pick_place

## Design notes
- Library skills are what Droid imitates: keep them short, narrated with `robot.note`,
  and in the exact style of `droid_template/API.md`.
- Every library file must pass `skillsmith validate` (CI runs it on all of them).
- Calibration writes `calibration/offsets.json` (gitignored on the GX10). The loop
  wraps `Kinematics.ik` to add the offset, so nothing shared changes.

## Acceptance (in `make check`)
- New library skills pass the validator and succeed on the fake env with tests in `tests/ws7/`.
- Calibration fitting is unit-tested with synthetic measurements (known offset recovered ±2 mm).
