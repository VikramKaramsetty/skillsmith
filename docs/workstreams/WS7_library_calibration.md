# WS7: library & calibration

**Branch prefix:** `ws7/` · **Owns:** `src/skillsmith/calibration/`, `skills/library/`, `tests/ws7/` · **Reads:** `docs/NORTH_STAR.md`, `docs/DEMO.md`, `droid_template/AGENTS.md`, `droid_template/API.md`

## Goal
Seed the library so the demo's failure is real and reliable, grow it so new tasks reuse old
skills, and handle consistent position offsets as calibration, not code.
Serves NORTH_STAR: 1, 2.

## Tasks (one PR each, in order)
- [ ] WS7.1 Demo seed skill: stack tuned for blocks (grips near the top) that genuinely slips on cups
- [ ] WS7.2 Library skills put_in, tower and sort_by_color
- [ ] WS7.3 Calibration: fit a repeated xy offset and apply it via OffsetKinematics
- [ ] WS7.4 Stretch: pyramid reusing stack

## Design notes
- WS7.1 is the demo's honest failure: a real `stack` skill written for blocks that grips near
  the top. On cups it slips in carry (SLIP_IN_CARRY), and Droid's fix is to grip at mid-height.
  It must pass the validator and succeed on blocks in the fake world.
- **The demo pair must not nest:** identical cups slide inside each other and "on top" fails.
  Use a smaller cup on an upturned cup, or a cup on a block (see `docs/DEMO.md`).
- Library skills are what Droid imitates: short, narrated with `robot.note`, `align_over` before
  descending, validator-clean, listed `uses`.
- A consistent offset (every place 1.5 cm left) is calibration (environment bucket), never a
  hard-coded number in a skill. Contract stub: `src/skillsmith/calibration/__init__.py`
  (`fit_offset`, `OffsetKinematics`); offsets in gitignored `calibration/offsets.json`.

## Acceptance (in `make check`)
- Every library skill passes `skillsmith validate` (CI checks all of them) and runs on the fake
  world in `tests/ws7/`; the WS7.1 skill succeeds on blocks and slips on cups in rim-slip mode.
- `fit_offset` recovers a synthetic offset within ±2 mm.
