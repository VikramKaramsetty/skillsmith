# WS5: Droid repair loop

**Branch prefix:** `ws5/` · **Owns:** `src/skillsmith/repair/`, `droid_template/`, `tests/test_repair_loop.py`, `tests/ws5/`
**Reads:** `docs/ARCHITECTURE.md` (loop + "Where the Droid runs"), `docs/INTERFACES.md` §6–7, `docs/SAFETY.md` layer 1, Factory docs: https://docs.factory.ai/cli/droid-exec/overview

## Goal
Real Droid fixes real failures, quickly and visibly. This is the Factory track: the
designed trigger (task failure), controlled permissions, and work shown on screen.

## Tasks (one PR each, in order)
- [ ] WS5.1 Run DroidSDKAuthor with real Droid end to end on the fake env and fix what breaks (tool ids, permission actions, structured output, session reuse)
- [ ] WS5.2 Fake droid_sdk module for CI plus plumbing tests for DroidSDKAuthor (rounds, session reuse, refusals, timeout, lessons)
- [ ] WS5.3 Seeded-bug benchmark: 6 broken skills, reporting fix rate, one-round fix rate and seconds-to-fix
- [ ] WS5.4 Failure-report tuning from benchmark results (snapshot, previous diffs, hints)
- [ ] WS5.5 Draft-path prompts for brand-new tasks (put_in, tower) using library skills
- [ ] WS5.6 Calibration trigger: repeated grasp xy error starts a Droid calibration run (with WS7)
- [ ] WS5.7 Lessons in action: show a lesson from one task changing Droid's first draft on the next, with a test
- [ ] WS5.8 Audit trail: commit each saved skill version with Droid's diagnosis and change as the commit message
- [ ] WS5.9 Stage profile for demo speed: 2 rounds, sim budget 1, 90 s per round, fast model, session warmed up before the demo
- [ ] WS5.10 Better images for Droid: cropped before/after pair plus 3-4 key frames (pickup, carry, place) attached to the repair call

## Design notes
- **Read `docs/NORTH_STAR.md` first.** WS5 *is* the award: Droid SDK doing real work,
  designed trigger, controlled touch, visible work.
- **Speed is a hard requirement:** one round, under 60 s, on the demo task. Every change is
  judged by one-round fix rate and seconds-to-fix from the WS5.3 benchmark. The stage profile
  (WS5.9) is `config/stage.yaml`, merged over `robot.yaml` with `--profile stage` (request the
  loader change from the lead); it adds `DroidSDKAuthor.warm_up(run_id)` to open the session early.
- Prompts ask for the smallest edit that fixes the stated failure, never a rewrite.
- The product path is `repair/droid_sdk.py` (`DroidSDKAuthor`, droid-sdk 0.5.0). The policy
  and tools live in `repair/droid_tools.py`. `droid_client.py` (CLI) is a fallback only.
- Keep the permissions story tight: autonomy LOW, `Execute` disabled, `decide()` allows only
  `skill.py` edits + `skillsmith` tools, workspace outside the repo. If WS5.1 finds the real
  tool IDs, set `droid.restrict_tools` in a CONTRACT CHANGE REQUEST.
- CI has no Droid binary or API key. The heavy job installs `droid-sdk`, so real SDK classes
  are testable (`tests/test_droid_tools.py`); WS5.2 adds a fake `droid_sdk.Session` that
  streams scripted events and edits `skill.py`.
- Benchmark bugs (WS5.3): rim grip, missing place height, wrong object, unreachable
  travel height, forgot release, infinite-ish loop. Track fix rate and median rounds;
  put the table in the PR. Target ≥ 5/6 fixed in ≤ 2 rounds.
- Speed matters on stage. Try a faster `droid.model`/`reasoning` and report latency per round.
- Never weaken a gate to make Droid look better. If Droid keeps fighting a rule, improve
  `droid_template/AGENTS.md` or the report.

## Acceptance (in `make check`)
- Shim-based tests for draft, repair, session continuation, non-zero exit, and timeout.
- `make demo-fake` still passes with `--author scripted`.
- A recorded run of `skillsmith demo-fake --author droid` attached to the WS5.2 PR (events.jsonl).
