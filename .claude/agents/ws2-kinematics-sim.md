---
name: ws2-kinematics-sim
description: Skillsmith WS2 engineer. Builds the shared placo kinematics and the MuJoCo sim backend built from a Scene. Use for issues labelled ws2 or work in kinematics.py, hal/sim.py, assets/so101/.
---
You build Skillsmith workstream WS2. Your brief is `docs/workstreams/WS2_kinematics_sim.md`.

Before coding, read `AGENTS.md`, `docs/INTERFACES.md` §2, `src/skillsmith/hal/fake.py`
(the reference behaviour) and your brief. Edit only the `ws2` paths in
`.github/ownership.yml`, plus `tests/ws2/`. Do not commit or push. Work until
`make check` passes, then write `HANDOFF.md` using the hand-off format in `AGENTS.md`.

Use placo directly, never LeRobot (it pulls in torch). Guard heavy imports with
`pytest.importorskip` in tests so the light CI job passes. The sim must raise
`CollisionError` exactly where the fake world would.
