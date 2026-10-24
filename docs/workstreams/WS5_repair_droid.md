# WS5: Droid repair loop

**Branch prefix:** `ws5/` · **Owns:** `src/skillsmith/repair/`, `droid_template/`, `tests/test_repair_loop.py`, `tests/ws5/`
**Reads:** `docs/ARCHITECTURE.md` (loop + "Where the Droid runs"), `docs/INTERFACES.md` §6–7, `docs/SAFETY.md` layer 1, Factory docs: https://docs.factory.ai/cli/droid-exec/overview

## Goal
Real Droid fixes real failures, quickly and visibly. This is the Factory track: the
designed trigger (task failure), controlled permissions, and work shown on screen.

## Tasks (one PR each, in order)
- [ ] WS5.1 Fake droid shim for CI, plus DroidAuthor plumbing tests (workspace layout, flags, session reuse, timeout)
- [ ] WS5.2 Run real droid exec against the fake env end to end and fix stream-json parsing of session id and final text
- [ ] WS5.3 Seeded-bug benchmark: 6 broken skills with a script that reports Droid's fix rate and rounds
- [ ] WS5.4 Failure-report tuning from benchmark results (snapshot, previous diffs, hints)
- [ ] WS5.5 Draft-path prompts for brand-new tasks (put_in, tower) using library skills
- [ ] WS5.6 Calibration trigger: repeated grasp xy error starts a Droid calibration run (with WS7)

## Design notes
- Keep the permissions story tight: `--auto low`, `--remove-tools Execute`, workspace
  outside the repo, only `skill.py` read back. Log the exact command in `droid.started`.
- CI has no Droid and no API key: WS5.1's shim is a tiny script on PATH that edits
  `skill.py` from a fixture and prints stream-json lines. Point `cfg.droid.bin` at it in tests.
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
