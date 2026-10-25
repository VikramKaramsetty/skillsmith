# Interfaces (the contracts)

Parallel agents can only work independently if these hold still. The source of truth
is the code in `src/skillsmith/types.py`, `hal/base.py`, `primitives.py`,
`repair/protocols.py` and `events.py`; this file explains them. **The lead owns this
file.** Ask for changes with a `CONTRACT CHANGE REQUEST` (see `AGENTS.md`).

Conventions: metres, seconds, joint degrees, gripper 0–100 (0 = closed). Robot base
frame: x forward, y left, z up, origin on the table under the shoulder_pan axis.

---

## 1. Shared data types (`skillsmith/types.py`)

```python
@dataclass(frozen=True)
class SceneObject:
    id: str          # "blue_cup_0" (stable within one Scene)
    label: str       # "blue cup"  (what a skill searches by)
    color: str       # "blue"
    kind: str        # "cup" | "block" | "ball" | ...
    x: float         # centre of the base, robot frame
    y: float
    z: float         # height of the object's BASE above the table (0 = on table)
    height: float    # m
    width: float     # m (diameter for cups/balls, edge for blocks)
    confidence: float = 1.0
    # property: top = z + height

@dataclass(frozen=True)
class Scene:
    objects: tuple[SceneObject, ...]
    t: float                    # unix seconds
    source: str                 # "fake" | "sim" | "camera:front" | ...
    image_path: str | None = None
    def find(query) -> SceneObject | None   # matches id, then label, then "color kind"
    def require(query) -> SceneObject       # raises SkillError with a clear message

@dataclass
class PrimitiveCall:            # one line of a trace
    index: int
    name: str                   # "move_to" | "grasp" | ...
    args: dict
    t: float
    ee_before: Vec3 | None
    ee_after: Vec3 | None
    ok: bool
    result: Any = None
    error: str | None = None

@dataclass
class SafetyIssue:  code: str; message: str; call_index: int | None = None
@dataclass
class SafetyReport: ok: bool; issues: list[SafetyIssue]
@dataclass
class VerifyResult: ok: bool; detail: str

@dataclass
class TaskSpec:
    text: str                   # "stack the blue cup on the red cup"
    skill: str                  # "stack"
    params: dict                # {"top": "blue cup", "bottom": "red cup"}
    success: dict               # predicate, see §5

@dataclass
class AttemptResult:
    ok: bool                    # verify.ok and no error and safety.ok
    env: str                    # "fake" | "sim" | "real"
    skill: str
    version: int
    trace: list[PrimitiveCall]
    error: str | None           # "SkillError: blue cup not found"
    verify: VerifyResult | None
    safety: SafetyReport
    scene_before: Scene | None
    scene_after: Scene | None
    duration_s: float
    video_path: str | None = None   # sim replay mp4 (sim env only)
    phases: list[PhaseCheck] = []   # filled by analysis.analyse_attempt
    classification: Classification | None = None
    keyframes: dict[str, str] = {}  # phase -> annotated image path

PHASES = ("approach", "grasp", "lift", "carry", "place", "release", "retreat")

@dataclass
class PhaseCheck:
    phase: str; ok: bool; detail: str; start_index: int; end_index: int
    frame_path: str | None = None

@dataclass
class Classification:
    cls: str            # key of FAILURE_CLASSES, e.g. "SLIP_IN_CARRY"
    bucket: str         # "skill" (Droid) | "environment" (human/calibration) | "unknown" (Droid, flagged)
    phase: str | None   # first failing phase
    call_index: int | None
    summary: str        # one line WITH numbers; leads Droid's evidence and the dashboard banner
    hint: str = ""      # where to look
    confidence: float = 1.0
    escalate: bool = False   # same class after a parameter-only change → "change the approach"
    evidence: dict = {}

FAILURE_CLASSES: dict[str, bucket]   # the full list lives in types.py
```

`SceneObject` also has `visible: bool = True` (False = hidden behind the arm; position is the
last one seen). `AttemptResult.reason()` leads with `"<CLASS>: <summary>"` when classified.

Exceptions: `SkillError` (skill-level, readable), `SafetyViolation(SkillError)`
(raised by the guard *before* motion), `Unreachable(SafetyViolation)` (IK failed),
`CollisionError(SkillError)` (sim/fake detected contact), `WorldChanged(SkillError)` (the world
model saw a not-held object move; raised before the next motion).

---

## 2. Hardware abstraction (`skillsmith/hal/base.py`)

Every backend provides these three protocols, bundled in an `Env`.

```python
class ArmBackend(Protocol):
    name: str
    def joints(self) -> list[float]                       # 6 values, deg; gripper 0..100 last
    def move_joints(self, target: Sequence[float], speed_dps: float) -> None   # blocking
    def set_gripper(self, pct: float) -> None             # blocking; 0 closed, 100 open
    def gripper_holding(self) -> bool                     # load/position heuristic
    def stop(self) -> None                                 # hold position now

class Kinematics(Protocol):
    def fk(self, joints: Sequence[float]) -> Vec3         # gripper tip position
    def ik(self, target: Vec3, seed: Sequence[float]) -> list[float] | None
        # gripper pointing down; returns 6 joints (gripper copied from seed) or None

class Perception(Protocol):
    def observe(self) -> Scene                            # the LATEST world state
    def snapshot(self, path: str) -> str | None           # save a frame, return path
    # optional (the real WorldModel, WS3; the Robot/classifier use them when present):
    def unexpected_motion(self) -> str | None             # "red cup moved 3.2 cm while not held"
    def perception_ok(self) -> tuple[bool, str]           # confidence gate for the classifier
    def align_offset(self, obj: SceneObject) -> tuple[float, float]   # wrist-camera fine dx, dy (m)
    def holding_check(self) -> tuple[bool, str]           # wrist-camera post-grasp check

@dataclass
class Env:
    name: str                   # "fake" | "sim" | "real"
    arm: ArmBackend
    kin: Kinematics
    perception: Perception
    def close(self) -> None

def make_env(name: str, cfg: Config, scene: Scene | None = None, bus: EventBus | None = None) -> Env
    # "sim" and "fake" accept a Scene to build the world from (sim mirrors the real table)
```

Rules:
- `move_joints` must not return until the arm is within tolerance, or it raises.
- Backends do **not** enforce the Cartesian envelope (that is `safety.py`), but the
  real backend and `robotd` **do** enforce joint limits and per-step deltas.
- `sim` and `fake` raise `CollisionError` on unintended contact.
- `move_joints` ignores the gripper value in `target`; only `set_gripper` moves the jaws.
- `ik` returns `None` for a target outside the reachable workspace (it never raises for that).

Frames (WS3, `skillsmith/perception/frames.py`) are the WorldModel's input:
`FrameSource.read() -> Frame` (JPEG/BGR plus capture time `t`, unix seconds) and `close()`.
`RobotdFrames` is the production source (only robotd opens cameras).

`robotd` HTTP API (WS1, on the Mac at `127.0.0.1:8100`, JSON bodies):

| Method | Path | Body / result |
|---|---|---|
| GET | `/state` | `{"joints":[6], "holding":bool, "gripper_pos":float, "gripper_load":float, "torque":bool, "t":float}` |
| POST | `/move_joints` | `{"target":[6], "speed_dps":float}` → `{"ok":true}` (blocking; the gripper value is ignored) |
| POST | `/gripper` | `{"pct":float}` → `{"ok":true, "holding":bool}` |
| POST | `/stop` | hold current position |
| POST | `/torque` | `{"on":bool}` → `{"ok":true, "torque":bool}` (Goal=Present before enabling) |
| GET | `/frame/{cam}` | JPEG with header `X-Frame-Time: <capture unix seconds>`; `cam` ∈ `wrist`, `front` |
| GET | `/stream/{cam}` | MJPEG for the dashboard; each part carries `X-Frame-Time` |

Errors: `{"ok": false, "error": str, ...}` with 400 (bad request, or `"outside": {...}` for a
target past a joint limit), 401 (token), 404 (unknown camera), 409 (refused: stopped, not
settled, or `"lag": {...}` when the watchdog halted a stalled move), 503 (bus error). When
`$ROBOTD_TOKEN` is set, every request must send it in the `X-Robotd-Token` header. The client
(`RobotdArm`, WS1.5) turns every non-2xx answer into an exception **and** an `error` event.

---

## 3. The skill API (`skillsmith/primitives.py`): what Droid can touch

A skill is one Python file:

```python
"""stack: put one object on top of another."""
SKILL = {"name": "stack", "params": {"top": "str", "bottom": "str"}, "uses": ["pick_place"]}

def run(robot, scene, skills, top, bottom):
    ...
```

`robot` (class `Robot`):

| Call | Meaning |
|---|---|
| `robot.move_to(x, y, z, speed=None, side=None)` | gripper tip to (x, y, z); `side=None` keeps the orientation (level after `approach_side`, else pointing down), True = level, False = down. Long vertical moves are split into ≤ 2 cm steps. `speed` in m/s, capped by config. |
| `robot.grasp() -> bool` | close the gripper; True if something is held |
| `robot.release()` | open the gripper fully |
| `robot.look() -> Scene` | fresh detection |
| `robot.home()` | go to the safe home pose |
| `robot.position() -> (x, y, z)` | current tip position (read-only) |
| `robot.holding() -> bool` | gripper load check (read-only) |
| `robot.note(text)` | message to the dashboard ("lowering onto red cup") |
| `robot.align_over(obj, hover=0.08) -> float` | hover above the object's **latest** position, fine-align with the wrist camera if available; returns the remaining xy error (m). Use before descending to grasp or place. |
| `robot.approach_side(obj, grip_z=None, hover=0.03) -> float` | side grasp: wrist-camera fix (`perception.wrist_frame()` + `kin.wrist_camera_pose()`, when both exist), then level open jaws lowered around the object to `grip_z` (default top - 2 cm). Returns the wrist correction (m). `grasp()` next. |

Before every call the Robot asks the world model for unexpected motion; if a not-held object
moved, the skill stops with `WorldChanged` (skills must not try to catch it).

`scene` (a `Scene`): `scene.find(q)`, `scene.require(q)`, `scene.objects`.
Objects have `.x .y .z .height .width .top .label .color .kind .id`.

`skills` (class `SkillCaller`): `skills.call("pick_place", obj="blue cup", x=0.2, y=0.0)`.
Max nesting depth 3.

Also available without import: `math`, and the builtins `abs min max round len range
float int str bool list dict tuple enumerate zip sorted sum any all`, plus
`SkillError`, `ValueError`, `RuntimeError` to raise.

Every call goes through the `SafetyGuard` and is recorded as a `PrimitiveCall`.

---

## 4. Static rules (`skillsmith/skills/validator.py`)

`validate(source) -> list[Issue]`; an empty list means pass. Rejects:
imports; `while`; `try`/`with`/`class`/`global`/`nonlocal`/`async`/`yield`/`lambda`;
any attribute starting with `_`; the names `eval exec compile open __import__ getattr
setattr delattr globals locals vars input breakpoint type object super`; `robot.*`,
`scene.*`, `skills.*` attributes outside the API above; using `robot`/`scene`/`skills`
other than as `robot.attr` or as a call argument (no aliasing); `range()` with a literal
bound > 50; files over 200 lines; a missing `def run(robot, scene, skills, ...)`.

Also (WS4.1, PR #143): names limited to the runtime's builtins, `math`, `SkillError` and names
the skill binds; no frame/generator introspection attributes, `.format` or `.mro`; handles
(`robot`/`scene`/`skills`) passed only to same-named parameters of local functions; no
attribute assignment and no mutation of robot/scene data; constant-folded `range` bounds; no
recursion; constant exponents with |e| ≤ 10, no `<<`, sequence repetition ≤ 1000; top-level
assignments constant only; no decorators. `validate()`'s signature is unchanged.

No `try` is deliberate: errors must reach the failure report, not get swallowed.

---

## 4b. Failure analysis (`skillsmith/analysis/`, WS4)

```python
def analyse_attempt(attempt: AttemptResult, cfg) -> tuple[list[PhaseCheck], Classification | None]
def should_escalate(previous: Classification | None, current: Classification,
                    last_change_type: str | None) -> bool
```

Called by `runtime.run_skill` after every attempt; the runtime emits `phase.check` and
`attempt.classified`. Rules only, no AI, milliseconds. A failed confidence gate
(`perception_ok()`) means an environment class, never a skill class. The `RepairLoop` sends
only *skill* and *unknown* failures to Droid.

---

## 5. Success predicates (`skillsmith/verify.py`)

JSON-able dicts evaluated on the post-attempt `Scene`:

```json
{"op": "on_top",  "args": ["blue cup", "red cup"]}        // xy < tol, a.z ≥ b.top - 1 cm
{"op": "inside",  "args": ["red block", "green cup"]}     // xy < cup radius, a.z < cup.top
{"op": "near",    "args": ["red block", 0.20, -0.10]}     // xy within tol of a point
{"op": "all",     "args": [ <predicate>, ... ]}
{"op": "any",     "args": [ <predicate>, ... ]}
{"op": "not",     "args": [ <predicate> ]}
```

`evaluate(pred, scene, xy_tol) -> VerifyResult` with a human-readable `detail`. The
detail goes straight into Droid's failure report, so it must be specific:
"blue cup is 4.1 cm from red cup in xy (limit 3.0 cm)".

---

## 6. Repair-loop plug points (`skillsmith/repair/protocols.py`)

```python
class SkillAuthor(Protocol):            # Droid lives behind this
    def draft(self, task: TaskSpec, ctx: AuthorContext) -> str
    def repair(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str

class Approver(Protocol):               # human gate before the real arm
    def request(self, run_id: str, summary: dict) -> tuple[bool, str]   # (approved, note)

@dataclass
class AuthorContext:
    run_id: str
    round: int
    scene: Scene | None
    snapshot_path: str | None
    library: dict[str, str]             # name → source, read-only
    attempt: AttemptResult | None = None    # the failed run (target attempt or failing sim gate)
    expected: AttemptResult | None = None   # a passing run of the same skill (sim gate), if any
    # both None after a static or reviewer rejection; Droid's evidence tools read them (WS5.4)
```

A `SkillAuthor` may expose `last_result: dict | None`, the structured result of its last
round (`DroidSDKAuthor` does); the loop reads `change_type` from it for escalation.

Implementations: `DroidSDKAuthor` (the product: Droid Python SDK, `repair/droid_sdk.py`),
`DroidAuthor` (`droid exec` CLI, stage-emergency fallback), `ScriptedAuthor` (tests/demo-fake);
`CliApprover`, `AutoApprover` (**refuses** when the target env is `real`),
`DashboardApprover` (WS6).

---

## 7. Events (`skillsmith/events.py`)

One JSON object per line in `runs/<run_id>/events.jsonl`, also pushed to subscribers
(the dashboard's SSE feed):

```json
{"t": 1730000000.123, "run_id": "20261024-153000-ab12", "type": "attempt.started", "data": {...}}
```

| type | data |
|---|---|
| `run.started` | `task` (TaskSpec dict) |
| `scene.observed` | `scene` (dict), `image_path` |
| `plan.selected` | `skill`, `source` ("library" \| "draft"), `version` |
| `attempt.started` / `attempt.finished` | `env`, `skill`, `version` / `+ ok, error, duration_s, reason, scene_before, scene_after` (Scene dicts or null), `safety` (SafetyReport dict or null), `perception` (`[ok, reason]` from `perception_ok()`, or null) |
| `primitive.call` | `PrimitiveCall` dict |
| `note` | `text` (from `robot.note`) |
| `verify.result` | `ok`, `detail` |
| `repair.started` | `round`, `reason` |
| `droid.started` | `round`, `via` ("droid-sdk" \| cli cmd), `workspace`, `autonomy`, `disabled_tools` |
| `droid.tools` | `tools` (ids available to Droid this session) |
| `droid.message` | `round`, `text` (Droid's plan/reasoning shown to the user) |
| `droid.tool_call` / `droid.tool_result` | `round`, `name`, `input` / `round`, `tool`, `is_error`, `content` |
| `droid.permission` | `round`, `action`, `target`, `allowed`, `reason` |
| `droid.check` | `round`, `tool`, `ok`, plus per tool: `validate_skill` (no more); `simulate_skill` `run`, `reason`; `write_skill` `lines`; `skill_source` `lines` (the loop wrote Droid's returned skill); `read_file` `reason` (only on a refusal) |
| `droid.result` | `round`, `subtype`, `result` {diagnosis, change, change_type ("parameter" \| "strategy"), sim_checked, confidence, lesson, skill_source}, `error` (RunFailure message or null), `session_id`, `duration` (seconds, float), `credits` |
| `droid.lesson` | `round`, `lesson` (appended to skills/lessons.md) |
| `droid.event` / `droid.finished` | CLI fallback only: raw stream-json event / `round`, `session_id`, `exit_code` |
| `skill.diff` | `old_version`, `new_version`, `diff` (unified) |
| `gate.result` | `gate` ("static" \| "sim" \| "safety"), `ok`, `detail` |
| `sim.video` | `path` |
| `reset.requested` | `message` (a human resets the table; followed by an approval request with `kind: "reset"`) |
| `approval.requested` / `approval.decided` | `summary` / `approved`, `note`. `summary.kind`: absent (approve code for the real arm), `"reset"`, or `"environment"` (+ `class`, `phase`, `summary`, `hint`, `skill`, `version`, `env`: a human fixes the table/camera, then approves a retry of the same code) |
| `skill.saved` | `skill`, `version` |
| `run.finished` | `ok`, `rounds` |
| `phase.check` | `PhaseCheck` dict |
| `attempt.classified` | `Classification` dict |
| `world.bumped` | `message` (raised as WorldChanged) |
| `error` | `where`, `message` (`where: "repair.environment"` when the loop stops on an environment failure) |

The dashboard must render from this stream alone, so a recorded `events.jsonl` can be
replayed as the demo fallback.
