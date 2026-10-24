# Demo (2 minutes)

Judges have little time and no context. The screen shows **one sentence per step**; everything
else sits behind a click for judges who ask.

## The task, designed to fail honestly

- The library holds a real `stack` skill **tuned for blocks**: it grips near the top (WS7.1). On
  a cup that grip slips. Nothing is faked: it's an existing skill that doesn't generalise, which
  is exactly the problem Skillsmith solves.
- **Objects that don't nest:** identical cups slide into each other. Use a small blue cup on an
  upturned red cup (or a block). Measure the cup against the jaw opening first.
- Rehearse until the classifier says `SLIP_IN_CARRY` every time and Droid fixes it in one round.

## Big-screen layout (WS6.2)

Task · status banner (`FAIL · SLIP_IN_CARRY at call #8`) · Droid's one-line diagnosis · the diff
(3–5 lines) · gate lights (static, sim, safety) · **Approve** · `PASS · stack v2 saved · lesson learned`.
The detail drawer (WS6.4) holds the annotated key frames, phase table, tool calls and permission decisions.

## Script

| Time | Screen / table | Say (one line each) |
|---|---|---|
| 0:00 | Arm, two cups, dashboard | "This arm has a block-stacking skill. It has never stacked cups." |
| 0:10 | Judge taps "stack the blue cup on the red cup" | "Its own failure is what triggers the AI." |
| 0:15 | The cup slips. Banner: **FAIL · SLIP_IN_CARRY at call #8** | "The camera caught it, and the system says exactly where and why." |
| 0:30 | Droid panel: diagnosis, its own sim test passing, a refused action, the short diff | "Droid, through the Factory SDK, can only edit this one file and call our tools. It just tested its fix in simulation." |
| 1:15 | Gates go green | "We re-check everything ourselves; we never trust the AI's word." |
| 1:25 | Demo owner presses **Approve** | "A human approves every real move." |
| 1:30 | The arm stacks the cup: **PASS · stack v2 saved · lesson learned** | "Fixed, saved, and remembered for next time." |
| 1:50 | Close on the timeline | "Designed trigger, controlled permissions, every step visible: the Droid SDK doing real work." |

If there's time (2:00–2:30): "build a tower" reuses the new skill. If the first attempt
succeeds outright, say so and ask the judge for a harder variant.

**Roles:** narrator, arm operator (hand on the power), demo owner on Approve.

## Talking points (Best Use of the Factory Droid SDK)

- *SDK in the running product:* point at README "Where and how Droid is used" and `repair/droid_sdk.py`.
- *Designed trigger:* the classifier labels a skill failure; environment failures go to a human instead.
- *Control:* an explicit allowlist, so Droid's only capabilities are our functions (show the
  allowlist and the `make droid-sandbox-check` output: shell, fs, fetch and path traversal all
  blocked); our code writes its fix; our gates; human approval.
- *Visible:* failure class, Droid's messages, tool calls, permission decisions, diff, diagnosis, events log.
- *Improvement:* the lesson line in `skills/lessons.md`; the second task reusing the skill; the measured
  numbers in `docs/RESULTS.md` (real Droid: 8/8 seeded bugs fixed in one round, median 17 s; sandbox proof).

## Q&A prep (honest answers)

- *"What if the camera is wrong?"* The confidence gate turns it into an environment failure, not a code change.
- *"Does Droid really see?"* It gets annotated key frames, crops and numbers, and can fetch more; geometry decides success.
- *"What if it bumps a cup?"* The live world model stops the skill with `WorldChanged`.
- *"Is this sandboxed?"* Six layers (`docs/SAFETY.md`); Python `exec` alone isn't a sandbox, and we don't rely on it.

## Fallback ladder

1. **Live, real arm.**
2. **Live, sim only** (`--env sim`): same dashboard, same Droid, the MuJoCo video replaces the arm.
3. **Replay**: a recorded `runs/<id>/events.jsonl` plus a screen recording, introduced as one.

## Pre-demo checklist

- [ ] GX10 up, `sudo dmesg | tail` clean; robotd `/state` responds; world model running at 10+ Hz.
- [ ] Camera taped down; `config/camera.json` fresh (re-run after any bump); marker drift ≈ 0.
- [ ] Lighting checked; detector finds both cups within 1 cm.
- [ ] USB-Ethernet link up; dashboard loads on the Mac.
- [ ] Droid logged in; stage profile on; **session warmed up**.
- [ ] Library reset: `stack` is the block-tuned v1; `skills/lessons.md` in its pre-demo state.
- [ ] Two rehearsals today; fallback recording on the desktop.
