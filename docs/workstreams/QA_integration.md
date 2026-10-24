# QA: integration tests

**Branch prefix:** `qa/` · **Owns:** `tests/integration/` · **Reads:** `docs/NORTH_STAR.md`, `AGENTS.md`, `docs/INTERFACES.md`

## Goal
Catch the bugs that live between workstreams right after each wave merges. QA writes failing
tests that pin a bug down and names the owning workstream; it does not fix other lanes' code.
Serves NORTH_STAR: 1, 3.

## Tasks (one PR each)
- [ ] QA.1 Event-schema conformance against INTERFACES section 7
- [ ] QA.2 Full repair loop on the sim env
- [ ] QA.3 Replay determinism for the dashboard
- [ ] QA.4 Protocol conformance suite for every Env backend
- [ ] QA.5 Classifier accuracy on recorded real failures once hardware runs exist

## Design notes
- QA.1: every event type emitted by a `make demo-fake` run exists in INTERFACES §7 with the listed fields.
- QA.4: one parametrised suite (ArmBackend, Kinematics, Perception) run against fake, sim, and the
  robotd mock, so every backend behaves the same.
- QA.5: label recorded real failures by hand, report the classifier's precision per class; a
  wrong label counts worse than UNKNOWN.

## Acceptance
Tests that need an unmerged workstream use `pytest.mark.skip(reason="waiting on WSn.m")`, so
`make check` stays green.
