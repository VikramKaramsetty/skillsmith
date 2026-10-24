# Research notes

Sources behind the design, and what we took from each. Preprints were read at
abstract/excerpt level only.

| Source | What it is | What we took |
|---|---|---|
| [Factory `droid exec` docs](https://docs.factory.ai/cli/droid-exec/overview) | Headless one-shot Droid runner | Read-only by default; `--auto low` = edits only; `--remove-tools Execute`; `-o stream-json` for live events; `--session-id` to continue across repair rounds; `-f` prompt file; `--cwd`. Exit 0 ≠ task success, so we verify ourselves. |
| [MuJoCo Menagerie `robotstudio_so101`](https://github.com/google-deepmind/mujoco_menagerie/tree/main/robotstudio_so101) | SO-101 MJCF (`so101.xml`, `scene.xml`, `scene_box.xml`) derived from TheRobotStudio's `so101_new_calib` | Sim model with gripper collision primitives tuned for manipulation. Actuator gains differ from upstream: tune for settling. |
| [LeRobot `kinematics.py`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/model/kinematics.py) / [placo](https://placo.readthedocs.io/en/latest/kinematics/getting_started.html) | `RobotKinematics(urdf_path, target_frame_name="gripper_frame_link")`, FK/IK in degrees via placo (Pinocchio) | Our IK for both sim and real, already installed on the GX10; position-weighted IK with a small orientation weight. |
| [HyCodePolicy (arXiv 2508.02629)](https://arxiv.org/abs/2508.02629) | Code-as-policies with sim execution + VLM checks at checkpoints, then program repair | The shape of our loop: run the code in sim, check outcomes visually, feed traces and observations back to repair. |
| [RoboInspector (arXiv 2508.21378)](https://arxiv.org/abs/2508.21378) | Analysis of why LLM policy code fails, plus failure-feedback refinement (up to +35% reliability) | Specific, structured failure feedback matters: our report has the exception line, trace table, scene diff and verify detail. |
| [Test-driven code policies (arXiv 2603.00455)](https://arxiv.org/pdf/2603.00455v1) | Controllers checked against PyTest suites before deployment; diagnostics drive edits | Gate before deploy: static + sim + safety checks run before anything reaches the arm. |
| [VLCP (arXiv 2608.16978)](https://arxiv.org/pdf/2608.16978) | Rewrites control code within an episode every K steps | Considered and rejected for v1: we rewrite between attempts (Act-Observe-Rewrite), which is simpler and safer on hardware. |
| [RHO (arXiv 2606.16458)](https://www.emergentmind.com/papers/2606.16458) | Argues multi-turn code generation at test time is too slow for real-time control | Never repair mid-motion: the arm stops, Droid repairs, gates re-run, then retry. Keep primitives fast and hand-written. |
| [Rerun](https://rerun.io) | Logging/viewer for multimodal robotics data with timeline replay | Optional engineer debug viewer; the judge-facing dashboard is custom (needs an Approve button). Pin the version. |

## Open questions to settle during the build

- Exact `droid exec -o stream-json` field names for the session id and final text (WS5:
  run once and tighten `_session_id` / `_final_text`).
- Whether `so101_new_calib` zeros match LeRobot calibrated degrees on *our* arm (G3 step 1).
- Gripper "holding" heuristic on STS3215 (present load vs position error): WS1 to measure.
