# Plan

What has to be true, in what order, how long things take, and what we cut if we're behind.
The objective and scope are in `docs/NORTH_STAR.md`; how we build in parallel is in `docs/WORKFLOW.md`.

## Gates

A gate is passed when its check is green on `main` (or, for hardware, when the arm operator
has run it), not when someone says so.

| Gate | Done when | Main tasks | Status |
|---|---|---|---|
| **G0** | Contracts merged, `make check` green | lead | **done** |
| **G1** | `make demo-fake` runs fail → repair → success | lead | **done** |
| **G2** | `pick_place` and the demo `stack` succeed in MuJoCo; sim renders video + phase frames | WS2.1–2.6 | |
| **G3** | **First hardware test**: scripted `pick_place` on the real arm moves a block to a taped target ±2 cm, 3/3, **and the signals are validated** (checklist below) | WS1.1–1.7, WS2.2, WS2.7, WS3.1–3.5, WS3.9 | |
| **G4** | Real Droid (SDK) repairs the demo failure **in sim**: classified `SLIP_IN_CARRY` → one round under 60 s → pass | WS4.4–4.5, WS5.1–5.3 | |
| **G5** | **Full loop on the real arm**: block-tuned `stack` slips on a cup → classified → Droid → gates → approve → real success → skill + lesson saved, shown on the dashboard big screen | everything above + WS6.1–6.3, WS7.1 | |
| **G6** | Demo rehearsed 3× end to end in under 2:30, fallback recording saved | demo owner | |

If G3 slips, nothing else matters: Droid doesn't get to write code for an arm that can't
reliably run our own.

## Critical path

```
WS1.1–1.5 robotd ─┐
WS2.2 IK + WS2.7 ─┼─▶ G3 (real scripted pick) ─┐
WS3.1–3.5 world ──┘                            ├─▶ G5 (full loop on real) ─▶ G6
WS4.4–4.5 classifier ─┐                        │
WS5.1–5.3 Droid+evid. ┼─▶ G4 (Droid fixes in sim)┘
WS2.1–2.6 sim ────────┘          WS6.1–6.3 dashboard + WS7.1 demo seed ─▶ G5
```

Everything not on this path is a Should or Stretch (`NORTH_STAR.md` scope).

## G3 signal validation checklist (arm operator)

These decide which failure classes the classifier can trust. A signal that fails its test is
not used, and those classes fall back to UNKNOWN.

| Signal | Test | Pass |
|---|---|---|
| IK correctness | jog each joint ±20°; compare FK with a ruler (WS2.7) | < 1 cm |
| Object position | cups on 5 taped marks under demo lighting (WS3.9) | < 1 cm |
| Gripper "holding" | 10 grasps of *our* cups + 10 empty closes; log position/load (WS1.7) | clearly separable |
| Slip visible | reproduce the rim-grip slip 5×; servo signal and wrist check both catch it | 5/5 |
| Bump detection | nudge a cup by hand mid-skill | `WorldChanged` within ~0.2 s |
| Placement error | 10 open-loop placements | under 1 cm → skip wrist alignment; otherwise build WS3.7 |
| Cup vs gripper | jaw opening vs cup diameter | the grasp in the demo seed actually fits |

## Time budget (one fail → repair → success cycle)

| Step | Estimate | Notes |
|---|---|---|
| Real attempt (pick, carry, place, home) | 15–20 s at 0.08 m/s; 25–30 s at 0.05 m/s | ~0.9 m of path + gripper + settling |
| + `align_over` ×2 (if used) | +2–4 s | wrist frame + correction |
| Failure analysis + evidence assembly | < 1 s | rules + drawing frames |
| **Droid round** | **30–60 s target** (60–120 s with a heavy reasoning model) | includes its own `simulate_skill`; warm session; fast model |
| Our gates: static + MuJoCo + safety | 3–8 s | sim faster than real time; video renders in the background |
| Frontier vision second opinion (optional) | 2–5 s | runs in parallel with Droid, so off the critical path |
| Human approval | 3–5 s | |
| Scene reset | 0–10 s | the live world model starts the retry from wherever the cup actually is; reset only if it tipped or left the workspace |
| Retry attempt | 15–20 s | |
| **Total** | **≈ 70–125 s** | fits a 2-minute demo if the first attempt starts by 0:15 |

The world model runs continuously at 10–15 Hz (YOLO-World ≈ 20–50 ms per frame on the old GX10,
not yet measured on the Mac; HSV ≈ 5 ms; **measure both**).

## Cost and compute

| Item | Where | Cost |
|---|---|---|
| robotd, world model, sim, orchestrator, dashboard | the lead's Mac (local) | free; YOLO + MuJoCo run on the M5 Pro (48 GB) |
| Droid rounds | Factory (via the SDK) | per-round credits, logged in `droid.result.credits`; WS5.6 reports cost per fix |
| Frontier vision check (stretch) | API | cents per call |
| Build-time agents | each teammate's Claude/Droid account | subscription usage, spread across accounts |

## Hour plan (relative to start, ~28 h)

| Hours | Humans | Agents (shared queue) |
|---|---|---|
| 0–1 | Lead: start loops on every machine. Arm operator: robotd on the Mac, mount the iPhone high and centred, tape the workspace, print the checkerboard and markers. | WS1.1, WS2.1, WS3.1, WS4.4, WS5.2, WS6.1 |
| 1–4 | Arm operator: WS2.7 IK check, WS1.7 gripper signal test. Demo owner: choose non-nesting objects, measure cup vs jaw. | Wave 1 against `fake` |
| 4–8 | Arm operator: camera calibration, WS3.9 accuracy, scripted pick at 0.05 m/s → **G3**. | WS2 → **G2**; WS4.5 classifier; WS5.1 real Droid |
| 8–12 | Integrate on the real arm: world model + robotd + sim gate. | WS5.3 evidence v2 → **G4**; WS6.2 big screen |
| 12–16 | Sleep in shifts; one person + agents on fixes. | WS3.7, WS5.4, WS5.6 benchmark |
| 16–20 | **G5** on the real arm with the demo task; tune tolerances and the stage profile. | bug fixes only |
| 20–24 | Feature freeze. Record the fallback run (events + video). | docs, tests |
| 24–28 | **G6**: rehearse 3×, Q&A prep. | — |

## Roles

- **Lead**: owns `NORTH_STAR.md`, contracts, `config/robot.yaml` and contract changes (merged by
  hand). Watches `make status`, unblocks `blocked` issues, re-briefs agents.
- **Arm operator**: owns the arm, the Mac's hardware setup, robotd, cameras and calibration. Runs every
  `needs-hardware` task. Hand on the power during real runs.
- **Demo owner**: owns `DEMO.md`, objects and table setup, the big-screen view, the pitch, the
  fallback recording. Presses Approve on stage.

## Cut list (cut from the top when behind)

1. Droid-written monitors
2. Frontier vision second opinion
3. Pyramid
4. Self-calibration (WS7.3); use a manual offset instead
5. Expected-vs-actual frames (keep annotated key frames)
6. Droid evidence tools beyond `simulate_skill`
7. YOLO-World (HSV only)
8. Wrist alignment (only if G3 placement error is under 1 cm)

Never cut: the classifier, the live world model's bump stop, the human approval, the gates.
