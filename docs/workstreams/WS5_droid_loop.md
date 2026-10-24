# WS5: Droid loop

**Branch prefix:** `ws5/` · **Owns:** `src/skillsmith/repair/`, `droid_template/`, `tests/test_repair_loop.py`, `tests/test_droid_tools.py`, `tests/ws5/` · **Reads:** `docs/NORTH_STAR.md` (first: WS5 *is* the award), `docs/ARCHITECTURE.md` (repair loop, evidence), `docs/INTERFACES.md` §6–7, `docs/SAFETY.md`, https://docs.factory.ai/sdk/python

## Goal
Real Factory Droid, through the Droid Python SDK, fixes real failures in one round, under 60 s,
with controlled permissions and every step visible. It gets evidence a person could diagnose
from (classification, phases, annotated frames, expected-vs-actual), can pull more itself, and
its lessons make the next task better.
Serves NORTH_STAR: 1, 2, 3.

## Tasks (one PR each, in order)
- [ ] WS5.1 Run DroidSDKAuthor with real Droid end to end on the fake env and fix what breaks
- [ ] WS5.2 Fake droid_sdk module for CI plus plumbing tests for DroidSDKAuthor
- [ ] WS5.3 Evidence v2 package: classification first, phase table, annotated key frames, expected-vs-actual pairs, lessons
- [ ] WS5.4 Droid evidence tools: get_frame(phase), get_object_track(name), compare_with_success(phase)
- [ ] WS5.5 Route environment-bucket failures to a human, add change_type to Droid's result, and a no-magic-offsets rule
- [ ] WS5.6 Seeded-bug benchmark: fix rate, one-round fix rate and seconds-to-fix per class, with and without evidence v2
- [ ] WS5.7 Stage profile: 2 rounds, sim budget 1, 90 s per round, fast model, session warm-up
- [ ] WS5.8 Lessons in action plus an audit-trail commit for each saved skill

## Design notes
- Product path: `repair/droid_sdk.py` (`DroidSDKAuthor`, verified against droid-sdk 0.5.0).
  Policy and tools: `repair/droid_tools.py` (`decide`, `RepairTools`). `droid_client.py` (CLI)
  is a stage-emergency fallback only.
- **Never widen the allowlist or weaken a gate** to raise the fix rate. Droid's capabilities are
  `restrict_tools` (ToolSearch, Script, WaitForScript, TodoWrite) + our `skillsmith` functions,
  autonomy HIGH (safe only because of the allowlist; re-run `make droid-sandbox-check` after any
  change). Custom tools are callable only from a Script in this Droid CLI.
- Evidence v2 (WS5.3) order: classification line → phase table → task/params → camera check →
  annotated key frames + cropped before/after pair (WS3.8) → expected-vs-actual (sim frames from
  WS2.5 or the last successful run) → trace → scene tables → earlier rounds → LESSONS.md.
  Images ride along in Droid's existing call, so they add no latency.
- WS5.5: environment-bucket classifications never reach Droid; the loop asks a human instead.
  `change_type: "parameter" | "strategy"` feeds `analysis.should_escalate`. Rule in
  `droid_template/AGENTS.md`: never add a constant to compensate for a position error
  (that's calibration, WS7.3).
- **Speed is a hard requirement:** one round < 60 s on the demo task. Stage profile =
  `config/stage.yaml` merged with `--profile stage` (request the loader change from the lead) plus
  `DroidSDKAuthor.warm_up(run_id)` to open the session before the demo.
- CI has no Droid binary or key: the heavy job installs droid-sdk so real SDK classes are
  testable; WS5.2's fake `Session` streams scripted events and edits `skill.py`.

## Acceptance (in `make check`)
- Fake-SDK tests: draft, repair, session reuse across rounds, refused actions, timeout, lessons.
- Evidence v2 golden test on a fake-world rim-slip failure; `make demo-fake` still passes.
- WS5.6 results table (fix rate, one-round fix rate, median seconds-to-fix per class) in the PR.
