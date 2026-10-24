# Demo

## The 3-minute script

| Time | On screen / table | Say |
|---|---|---|
| 0:00 | Arm at home, cups and blocks on the table, dashboard open | "This arm knows five safe moves. It has never done what you're about to ask." |
| 0:15 | Judge picks a task card, e.g. **"stack the blue cup on the red cup"** | "Droid writes the program; the arm never sees a neural net in its motor loop." |
| 0:25 | Attempt runs on the real arm; camera check shows **FAIL** with the reason (e.g. "blue cup is 9.4 cm from red cup") | "It failed, and the failure is the trigger." |
| 0:45 | Dashboard: Droid's stream (reads failure report, trace, snapshot), then the **code diff** | "Droid reads the camera snapshot and the trace and edits only this one file." |
| 1:15 | Gates go green one by one: **static**, **sim** (MuJoCo replay video), **safety** checklist | "Before anything moves: allowed calls only, a simulation on a copy of this table, workspace and speed limits." |
| 1:40 | Human presses **Approve** | "A person approves every real run." |
| 1:50 | Real arm succeeds; camera check **PASS**; "skill saved: stack v2" | "Now it's a skill. The arm is permanently more capable." |
| 2:15 | Second card: **"build a tower"**. The plan reuses `stack` from the library and runs | "New task, old skill: the library compounds." |
| 2:45 | Close on the run timeline | "Droid wrote and repaired that code. We wrote the safety it runs inside." |

If the first attempt succeeds outright, say so, then ask the judge for a harder variant
(a cup on a block, an object into a cup).

## Demo tasks

Good: stack cups; sort blocks by colour into zones; build a small tower (2–3 blocks);
put a block inside a cup.
Avoid: anything soft or deformable, anything needing millimetre precision, transparent
or very shiny objects (colour detection), objects wider than ~8 cm (gripper).

Prepare printed task cards for these, and keep the parser templates in `tasks.py` in
sync with the wording.

## Fallback ladder

1. **Live, real arm** (target).
2. **Live, sim only**: `--env sim`. Same dashboard and same Droid; MuJoCo video replaces
   the arm. Use this if the arm, robotd or the camera misbehaves.
3. **Replay**: the dashboard replays a recorded `runs/<id>/events.jsonl`, plus a screen
   recording of a successful real run. Use this if the network or Droid is down.

Switch rungs without apologising: "Here's the same loop from our run an hour ago."

## Pre-demo checklist

- [ ] GX10 up, no NVIDIA errors in `sudo dmesg | tail`; robotd running; `/state` responds.
- [ ] USB-Ethernet link between the Mac and the GX10; dashboard loads over it.
- [ ] Droid authenticated (`droid exec "say ok"` works) on whichever machine runs it.
- [ ] Camera fixed, homography fresh (re-run after any bump), lighting checked; HSV
      thresholds tested on the actual objects.
- [ ] Objects at their taped start positions; workspace clear.
- [ ] `max_tcp_speed` at the rehearsed value; stop button within reach of the operator.
- [ ] Library reset to the demo state (`stack` absent or at the deliberately weak version).
- [ ] Fallback `events.jsonl` and video ready to open in one click.
- [ ] Arm supported by hand before any restart (torque off = limp).

## Track talking points

**Factory Droid**
- *Designed trigger*: Droid runs only when the camera says a task failed (or a gate
  rejects code). That's the event-driven trigger, not a chat window.
- *Controlled permissions*: `--auto low`, command execution removed, one writable file,
  then AST, sim, safety and human gates that Droid cannot touch.
- *Visible work*: every Droid tool call, diff and gate result streams to the dashboard
  and is saved in `events.jsonl`.

**Most Technically Complex**: an agent, physics simulation, vision, IK and real hardware
in one closed loop, with a skill library that grows.

**Best Hardware Hack**: the arm is the point. It writes its own skills, and the camera
grounds every claim of success in the physical world.
