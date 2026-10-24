# QA: integration tests

**Branch prefix:** `qa/` · **Owns:** `tests/integration/`
**Reads:** `AGENTS.md`, `docs/INTERFACES.md`

## Goal
Catch the bugs that live between workstreams, right after each wave merges. QA writes
failing tests and files them; it does not fix other workstreams' code.

## Tasks (one PR each)
- [ ] QA.1 Event-schema conformance test: every emitted event type and field matches INTERFACES section 7
- [ ] QA.2 Full repair loop on the sim env once WS2 lands, mirroring test_repair_loop on fake
- [ ] QA.3 Replay test: a recorded events.jsonl drives the dashboard to the same final state
- [ ] QA.4 Protocol conformance: every Env backend passes the same ArmBackend, Kinematics and Perception test suite

## Acceptance
Tests that need a missing workstream are `pytest.mark.skip(reason="waiting on WSn")`
until it merges, so `make check` stays green.
