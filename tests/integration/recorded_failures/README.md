# Recorded real failures (QA.5)

The classifier (`skillsmith.analysis`, WS4.5) is scored here against failures recorded on the
**real arm** and labelled by a human who watched them. `tests/integration/test_classifier_accuracy.py`
skips until this directory holds at least one labelled run.

## Adding a recording (human, after a hardware session)

1. Copy the run's event log here, keeping the run id as the directory name:
   `runs/<run_id>/events.jsonl` → `tests/integration/recorded_failures/<run_id>/events.jsonl`.
   Images are not needed; the classifier reads only the events.
2. Add an entry to `labels.yaml` with what *you saw happen*, not what the dashboard said:

   ```yaml
   - run: 20261024-153000-ab12
     attempt: -1          # which failed attempt in the run; -1 = the last one, 0 = the first
     label: SLIP_IN_CARRY # a key of skillsmith.types.FAILURE_CLASSES
     note: rim grip, blue cup slid out on the 16 cm carry to the red cup
   ```

   If you cannot tell what happened, label it `UNKNOWN`. Environment problems (camera knocked,
   object moved by hand, calibration off) get an environment class, never a skill class.
3. Run the report: `uv run python tests/integration/qa_classifier_accuracy.py tests/integration/recorded_failures`
   and `uv run pytest tests/integration/test_classifier_accuracy.py -s`.

Only `env == "real"` attempts are accepted; fake and sim runs are covered by WS4's own tests.

## What the test enforces

- Every recording is complete: `attempt.finished` carries `safety` (a report, not null) and
  `scene_after` (a Scene or an explicit null). Replay never fills a gap: an absent safety report
  is not a clean one, and a `look()` mid-skill is not the scene after it. An incomplete recording
  is listed and fails the test; it is never scored. Record again once the runtime emits both.
- No failed attempt comes back with no classification.
- No environment failure gets a skill label (that would send a table problem to Droid as code).
- Precision per predicted class is at least `MIN_PRECISION`; `UNKNOWN` is an abstention and never
  lowers precision. Recall per true class (human `UNKNOWN` excluded) is at least `MIN_RECALL`, so
  abstaining on a whole class does not pass. Overall score (correct +1, UNKNOWN 0, wrong −2) is above zero.
- Every recorded `SLIP_IN_CARRY` (the G5 demo failure) is labelled `SLIP_IN_CARRY`.
