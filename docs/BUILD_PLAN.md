# Build plan

## Gates

A gate is passed when its check is green on `main`, not when someone says so.

| Gate | Meaning | Check | Status |
|---|---|---|---|
| **G0** | Contracts merged | `types.py`, `hal/base.py`, `primitives.py`, `repair/protocols.py`, `events.py`, `docs/INTERFACES.md` exist; `make check` green | **done** |
| **G1** | Fake loop end to end | `make demo-fake` prints SUCCESS after 1 repair round | **done** |
| **G2** | pick_place in MuJoCo | `skillsmith run "move the green block to 0.15, 0.12" --env sim --author scripted --approver auto` succeeds; sim video written | WS2 |
| **G3** | **First test**: hand-scripted pick-and-place on the real arm using IK + camera | `skills/library/pick_place.py` moves a real block to a taped target ±2 cm, 3 runs out of 3 (see `RUNBOOK_GX10.md`) | human + WS1/2/3 |
| **G4** | Real Droid repairs a failing skill in sim | `skillsmith run "stack the blue cup on the red cup" --env sim --author droid --approver cli` goes fail → Droid edit → pass | WS5 |
| **G5** | Full loop on the real arm | Same task with `--env real --approver dashboard`: real fail → Droid → static/sim/safety green → approve → real success → saved | everyone |
| **G6** | Demo ready | Script in `DEMO.md` rehearsed 3× end to end; fallback `events.jsonl` + screen recording saved | demo owner |

If G3 slips, nothing else matters: the arm must reliably do one scripted pick before
Droid is allowed to write anything for it.

## Hour-by-hour (relative to hackathon start, ~28 h)

| Hours | Humans | Agents |
|---|---|---|
| 0–1 | Lead: push repo, run `make check`, start Wave 1. Arm operator: start robotd skeleton testing with teleop off, tape the workspace, place ArUco markers. | Wave 1 launch: WS1, WS2, WS3, WS4, WS6 |
| 1–4 | Arm operator: verify joint signs/offsets (RUNBOOK §G3 step 1–2). Demo owner: buy/pick objects, fix camera mount. | Wave 1 building against `fake` |
| 4–6 | Lead watches `make status`, unblocks Wave 1 (CI merges green PRs). Arm operator: homography calibration. | **G2** |
| 6–9 | Arm operator runs pick_place on real at 0.05 m/s. | Wave 2: WS5 (real Droid via the SDK), QA (sim integration) → **G3**, **G4** |
| 9–13 | Integration on real arm: robotd + camera + sim + Droid. | Fixes from hardware hand-offs |
| 13–17 | Sleep in shifts (keep one person + agents on fixes). | WS7 stretch, dashboard polish |
| 17–21 | **G5**: full loop on real with demo tasks; tune verify tolerances. | Bug fixes only |
| 21–24 | Freeze features. Record fallback run (events + video). | Docs, tests |
| 24–28 | **G6**: rehearse 3×, pitch, judge Q&A prep. | — |

## Human roles

- **Lead**: owns `docs/NORTH_STAR.md`, contracts, `config/robot.yaml` and every
  `CONTRACT CHANGE REQUEST` (merged by hand). CI auto-merges everything else; the lead
  watches `make status`, unblocks `blocked` issues and re-briefs agents.
- **Arm operator**: owns the physical arm, GX10, robotd and cameras. Runs every hardware
  test that agents hand off, and is the hand on the stop button during real runs.
- **Demo / dashboard owner**: owns `DEMO.md`, the objects and table setup, dashboard
  look, the pitch, and the fallback recording. Second pair of eyes on approvals.

With fewer people, merge the lead and demo roles; never merge the arm operator role
with anything during a real run.

## Cut list (drop in this order if behind)

1. Open-vocabulary vision model on the GX10 (HSV colour is enough for coloured cups/blocks).
2. Learned grasp primitive (GX10 stretch).
3. Calibration skill (Droid tuning grasp offsets).
4. "Build a pyramid" (keep "build a small tower" for library reuse).
5. Wrist-camera fine alignment (front camera only).
6. Live MuJoCo video on the dashboard (show the gate result and trace instead).

Never cut: the safety gates, human approval, the camera verify, the event log.
