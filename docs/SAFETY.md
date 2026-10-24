# Safety

Droid writes code that moves a real arm. Seven independent layers stand between a bad
line of code and a bad motion. Any one layer stops most mistakes; together they are the
story we tell the Factory judges ("controlled permissions").

| # | Layer | Where | Stops |
|---|---|---|---|
| 1 | **Droid tool allowlist** (Droid SDK `restrict_tools`): only ToolSearch, Script, WaitForScript, TodoWrite + our `skillsmith` functions; `read_file`/`write_skill` are workspace-only; the fix is written by our code; permission handler as backstop. Verified live by `make droid-sandbox-check` (shell, fs, fetch, path traversal all blocked) | `repair/droid_sdk.py`, `repair/droid_tools.py`, `tools/droid_sandbox_check.py` | Droid running commands, reading or writing outside its workspace, reaching the network, touching our code or checks |
| 2 | **Static check**: AST allow-list (only `robot.*`/`scene.*`/`skills.call`; no imports/while/try/dunder/eval), ≤ 200 lines, bounded loops | `skills/validator.py` | escaping the API, infinite loops, swallowed errors |
| 3 | **Restricted runtime**: `exec` with a tiny builtins table; only `Robot`/`Scene`/`SkillCaller` objects passed in | `skills/runtime.py` | anything the AST check missed (defence in depth, not a sandbox) |
| 4 | **Cartesian guard** before every motion: workspace box, min tip height, reach ring, TCP speed cap, call budget, IK reachability | `safety.py`, `primitives.py` | out-of-workspace or too-fast targets; nothing moves on a violation |
| 5 | **Simulation gate**: the exact code runs in MuJoCo on a copy of the real scene; any unintended contact or failed verify goes back to Droid | `hal/sim.py`, `repair/loop.py` | collisions, knocked-over objects, wrong logic |
| 6 | **Human approval** on the dashboard (code diff, sim video, safety checklist) | dashboard / `CliApprover` | anything that looks wrong to a person |
| 7 | **robotd**: joint limits from calibration, per-command delta clamp (`max_step_deg`), Goal=Present before torque-on, `/stop` | `robotd/` (WS1) | IK bugs, bad calibration, runaway commands |
| 8 | **Live bump stop**: the world model flags a not-held object moving more than 1 cm; the Robot raises `WorldChanged` before the next motion | `perception/world.py` (WS3), `primitives.py` | pushing on after knocking something over |
| 9 | **Failure routing**: environment-bucket failures (camera moved, out of reach, perception uncertain, hardware) go to a human, never to Droid as code | `analysis/` (WS4), `repair/loop.py` | Droid "fixing" code for a setup problem |

## Rules that do not bend

- `AutoApprover` refuses to exist for `env=real`. Real runs always need a human click.
- `RepairLoop` refuses to target `real` without a sim check env while
  `safety.require_sim_before_real` is true (the default).
- Library skills were approved before, so they run on real without re-simulation. If a
  library skill fails on real, its repair goes through every gate again.
- Agents building this repo never run anything against the real arm (see `AGENTS.md`).

## Physical safety (humans)

- One person is the **arm operator** at every real run: hand near the power switch (or
  the dashboard's robotd `/stop` button), eyes on the arm, not the screen.
- Disconnecting turns torque off and the arm goes limp: **support it** before stopping
  robotd or pulling USB.
- Clear the workspace of anything not in the task. No hands inside the box while torque
  is on.
- Start each session at `max_tcp_speed: 0.05` and raise it only after a clean run.
- Watch the GX10 for NVIDIA "bad register read" errors (`sudo dmesg | tail`), which
  ended a previous boot. If they appear, stop real runs.

## Known gaps (be honest in Q&A)

- Python `exec` with stripped builtins is not a security sandbox. That is acceptable here
  because layers 1, 2 and 4–7 don't depend on it.
- The fake world's physics is deliberately simple; only the MuJoCo gate is a meaningful
  collision check.
- The camera sees positions, not forces. A cup can be "on top" and still unstable.
