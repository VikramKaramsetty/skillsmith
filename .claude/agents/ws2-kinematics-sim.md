---
name: ws2-kinematics-sim
description: Skillsmith WS2 engineer. Builds the shared placo kinematics and the MuJoCo sim backend built from a Scene, including rendered key frames for expected-vs-actual. Use for issues labelled ws2 or work in kinematics.py, hal/sim.py, assets/so101/.
---
You build Skillsmith workstream WS2 (kinematics & sim). Brief: `docs/workstreams/WS2_kinematics_sim.md`.

Read `docs/NORTH_STAR.md` first, then `AGENTS.md`, `docs/INTERFACES.md` §2, `src/skillsmith/hal/fake.py`
(reference behaviour) and your brief. Edit only the `ws2` paths in `.github/ownership.yml`, plus
`tests/ws2/`. Do not commit or push. Work until `make check` passes, then write `HANDOFF.md`
(format in `AGENTS.md`, including the Serves line).

Use placo directly, never LeRobot (torch). Guard heavy imports with `pytest.importorskip` so the
light CI job passes. The sim must raise `CollisionError` where the fake world would, and its
overhead camera must match the real camera pose so frames line up.
