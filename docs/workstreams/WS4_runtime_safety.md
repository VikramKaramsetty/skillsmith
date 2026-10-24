# WS4: skill runtime + safety hardening

**Branch prefix:** `ws4/` · **Owns:** `src/skillsmith/skills/validator.py`, `skills/runtime.py`, `safety.py`, `verify.py`, `tasks.py`, `tests/test_validator.py`, `tests/test_safety_verify.py`, `tests/ws4/`
**Reads:** `docs/INTERFACES.md` §3–5, `docs/SAFETY.md`

## Goal
Make layers 2–4 of `docs/SAFETY.md` hard to get around, and give the camera check and
the task parser enough coverage for every demo task.

## Tasks (one PR each, in order)
- [ ] WS4.1 Adversarial validator tests (escape attempts via dunders, aliasing, f-string tricks) and fix any gaps
- [ ] WS4.2 Wall-clock timeout per attempt that stops the arm and reports which line was running
- [ ] WS4.3 Static carry-path check in safety.py so a held object never sweeps through a known object at travel height
- [ ] WS4.4 More success predicates: tower, sorted_by_color, left_of, upright, with readable details
- [ ] WS4.5 Task parser templates for sort, tower and put-in, plus a pluggable fallback hook for unparsed tasks
- [ ] WS4.6 Step-level post-grasp check: after grasp(), call the perception holding check and fail mid-skill with its reason (lead adds the hook)

## Design notes
- The validator is an allow-list; keep it one. Every new rejection needs a test whose
  message tells Droid exactly what to do instead.
- Timeout: run the skill in a worker thread; on timeout set a flag that `Robot._record`
  checks before each call (raise `SafetyViolation`), then call `env.arm.stop()`.
- Carry-path check: for each move_to while holding, sample the straight segment; the
  held object's base (tip z + grip offset) must clear every other known object's top
  by `safety.min_clearance` (lead adds the config key, so request it).
- Predicate details go verbatim into Droid's failure report: numbers, units, names.

## Acceptance (in `make check`)
- All existing tests pass, plus at least 15 new adversarial validator cases, a timeout
  test on the fake env, a carry-path test, and a test per new predicate and template.
