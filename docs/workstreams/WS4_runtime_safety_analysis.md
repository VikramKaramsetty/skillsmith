# WS4: runtime, safety & failure analysis

**Branch prefix:** `ws4/` · **Owns:** `src/skillsmith/skills/validator.py`, `skills/runtime.py`, `safety.py`, `verify.py`, `tasks.py`, `src/skillsmith/analysis/`, `tests/test_validator.py`, `tests/test_safety_verify.py`, `tests/ws4/` · **Reads:** `docs/NORTH_STAR.md`, `docs/ARCHITECTURE.md` (failure analysis), `docs/INTERFACES.md` §3–5, `docs/SAFETY.md`

## Goal
Make the safety layers hard to get around, and turn every failed attempt into a precise,
honest label: which phase failed, at which call, with which numbers, and who should fix it.
This label leads Droid's evidence and the dashboard banner, so it is what makes one-round
repairs possible.
Serves NORTH_STAR: 1, 2, 3, 4.

## Tasks (one PR each, in order)
- [ ] WS4.1 Adversarial validator tests and fixes
- [ ] WS4.2 Wall-clock timeout per attempt that stops the arm and names the running line
- [ ] WS4.3 Static carry-path check so a held object never sweeps through a known object
- [ ] WS4.4 Phase segmentation of a trace with a check per phase
- [ ] WS4.5 Failure classifier: classes, three buckets, confidence gating and a hint, tested per class on the fake world
- [ ] WS4.6 Escalation: same class after a parameter-only change says change the approach
- [ ] WS4.7 Predicates tower, sorted_by_color, left_of and upright with readable details
- [ ] WS4.8 Task parser templates for sort, tower and put-in, plus a fallback hook

## Design notes
- Contract stub: `src/skillsmith/analysis/__init__.py` (`analyse_attempt`, `should_escalate`).
  `runtime._analyse` already calls it and emits `phase.check` / `attempt.classified`; the report
  already leads with the classification. Classes and buckets live in `types.FAILURE_CLASSES`.
- **Plain rules, no AI**, over measured signals only: the trace (failing call, exception type),
  gripper signals (grasp result, `holding()`), scene before/after positions, verify numbers,
  safety issues, and `perception_ok()` from the WorldModel when present.
- Phases from the trace: approach (moves before the first grasp) · grasp · lift (first upward
  move while holding) · carry (horizontal moves while holding) · place (descent while holding) ·
  release · retreat. Each gets ok/detail ("lost blue cup during 16 cm sideways move").
- Buckets: **skill** → Droid repairs; **environment** → a human or calibration (Droid is not
  called); **unknown** → Droid with all evidence, flagged low confidence.
- **A wrong label is worse than UNKNOWN.** Fire a class only when the evidence is unambiguous;
  low perception confidence or marker drift always means PERCEPTION_UNCERTAIN / CAMERA_MOVED.
- Summaries carry numbers ("grasp at z=0.07 = 88% of cup height; lost during 16 cm move") and a
  hint ("grip height"). The same class repeating after a "parameter" change sets `escalate`.
- The FakeWorld reproduces most classes for tests (rim slip, collision, grasp miss, out of reach).
- Validator stays an allow-list; every rejection message says what to do instead. Timeout: a
  worker thread plus a flag checked in `Robot._record`, then `env.arm.stop()`.

## Acceptance (in `make check`)
- ≥ 15 new adversarial validator cases; timeout and carry-path tests on the fake world.
- One fake-world test per classifier class that the fake can produce, asserting class, bucket,
  phase and call index; a low-confidence perception stub yields an environment class.
- Tests per new predicate and parser template.
