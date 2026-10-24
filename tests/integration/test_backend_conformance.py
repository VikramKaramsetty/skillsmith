"""QA.4: one protocol conformance suite (INTERFACES §2) run against every Env backend.

A skill must run unchanged on fake, sim and the real arm, so each backend has to behave the
same at the seams the Robot and the repair loop rely on. Every check here is parametrised over:

- ArmBackend: `fake`, `sim` (WS2.4) and `robotd` (WS1). `robotd` is the real robotd HTTP server
  on 127.0.0.1 over a MockBus, driven by a local client that follows the HTTP table in INTERFACES
  §2; `robotd-client` is the same server driven by WS1's RobotdArm (WS1.5) once it lands.
- Kinematics: `fake`, `sim` (WS2.4) and `placo` (WS2.2, the one the real arm uses).
- Perception: `fake`, `sim` (WS2.4) and `worldmodel` (WS3.5, fed robotd's mock camera).

Backends that are not built yet skip with "waiting on WSn.m", so `make check` stays green; the
moment one lands, the whole suite runs against it. When a check fails for one backend and passes
for another, the bug lives in that backend (or the contract is ambiguous: ask the lead).
"""

from __future__ import annotations

import functools
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence, Tuple, Type

import pytest
from qa_extras import built, require

from skillsmith.config import load_config
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.types import CollisionError, Scene, SceneObject

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # robotd lives at the repo root, outside src/
    sys.path.insert(0, str(REPO_ROOT))

from robotd.controller import ArmController  # noqa: E402
from robotd.mock import MockBus, MockCamera  # noqa: E402

TIP_TOL_M = 0.005  # INTERFACES: IK may leave at most 5 mm of position error
SCENE_TOL_M = 0.01  # perception: an untouched object reads back within 1 cm
HELD_TOL_M = 0.015  # a carried / dropped object lands within 1.5 cm of where it should be
LIFT_M = 0.08

WORKSPACE_POINTS = [(0.18, 0.0, 0.15), (0.22, 0.08, 0.10), (0.15, -0.10, 0.05), (0.25, 0.0, 0.03)]
UNREACHABLE = (2.0, 0.0, 0.10)


# ---- what each backend needs to be built ----------------------------------------------------

@functools.cache
def _sim_error() -> Optional[Exception]:
    """None if make_sim_env builds an Env, else why not."""
    try:
        env = make_env("sim", load_config(), _scene())
    except (NotImplementedError, ImportError) as e:
        return e
    env.close()
    return None


def _placo():
    from skillsmith.kinematics import make_kinematics

    try:
        return make_kinematics(load_config())
    except (NotImplementedError, ImportError) as e:
        built(e, "kin", "waiting on WS2.2 (PlacoKinematics)")


@functools.cache
def _scene() -> Scene:
    """The demo table (two cups and a block) that both fake and sim are built from."""
    return FakeWorld.demo(load_config()).observe()


def _scene_env(name: str) -> Env:
    if name == "sim":
        built(_sim_error(), "sim", "waiting on WS2.4 (make_sim_env)")
    return make_env(name, load_config(), _scene())


# ---- robotd over real HTTP ------------------------------------------------------------------

GRASP_PAN = 20.0  # robotd mock: an object sits between the jaws when shoulder_pan >= this
HARD_STOP_ELBOW = 60.0  # robotd mock: elbow_flex jams here (a stalled joint)
OBJECT_AT = 40.0  # gripper position where the jaws meet that object


class _TableBus(MockBus):
    """MockBus with a tiny world: an object at pan >= GRASP_PAN and an elbow hard stop."""

    def write_goal(self, goal: Sequence[float]) -> None:
        self.object_at = OBJECT_AT if goal[0] >= GRASP_PAN else None
        super().write_goal(goal)
        self.present[2] = min(self.present[2], HARD_STOP_ELBOW)


@contextmanager
def _robotd_server(max_step_deg: float = 30, max_lag_deg: Optional[float] = None) -> Iterator[str]:
    """robotd's own FastAPI app under uvicorn on a free loopback port; yields the base URL."""
    require("fastapi", "robotd")
    uvicorn = require("uvicorn", "robotd")
    from robotd.server import create_app

    ctl = ArmController(_TableBus(), max_step_deg=max_step_deg, max_speed_dps=10_000, settle_timeout_s=0.3,
                        max_lag_deg=max_lag_deg)
    app = create_app(ctl, {"front": MockCamera("front"), "wrist": MockCamera("wrist")})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("robotd (mock) did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class RobotdHttpError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> {status}: {body}")
        self.status = status
        try:
            self.body = json.loads(body)
        except ValueError:
            self.body = {"raw": body}


class _RobotdHttpArm:
    """Local ArmBackend over the robotd HTTP table in INTERFACES §2 (stands in until WS1.5)."""

    name = "real"

    def __init__(self, url: str):
        self.url = url

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RobotdHttpError(method, path, e.code, e.read().decode()) from e

    def joints(self) -> List[float]:
        return [float(v) for v in self._call("GET", "/state")["joints"]]

    def move_joints(self, target: Sequence[float], speed_dps: float) -> None:
        self._call("POST", "/move_joints", {"target": list(target), "speed_dps": speed_dps})

    def set_gripper(self, pct: float) -> None:
        self._call("POST", "/gripper", {"pct": pct})

    def gripper_holding(self) -> bool:
        return bool(self._call("GET", "/state")["holding"])

    def stop(self) -> None:
        self._call("POST", "/stop", {})


def _robotd_client(url: str):
    import skillsmith.hal.real as real

    cls = getattr(real, "RobotdArm", None)
    if cls is None:
        pytest.skip("waiting on WS1.5 (RobotdArm)")
    return cls(url)  # QA assumes RobotdArm(base_url); if WS1.5 picks another signature, wire it here


# ---- ArmBackend -----------------------------------------------------------------------------

@dataclass
class ArmCase:
    """One backend plus the poses that mean the same thing on it (its joints have its own units)."""

    arm: object
    reachable: List[float]  # a target the arm can reach from where it starts
    grasp_path: List[List[float]]  # the last pose has an object between the open jaws
    empty_pose: List[float]  # nothing between the jaws here
    blocked: Callable[[], List[float]]  # sets up a world where moving to the returned target must raise
    blocked_exc: Type[BaseException]
    joint_tol: float  # "within tolerance" after move_joints, in the backend's joint units
    cleanup: List[Callable[[], None]] = field(default_factory=list)


def _scene_arm_case(name: str) -> ArmCase:
    env = _scene_env(name)
    arm, kin = env.arm, env.kin
    start = arm.joints()

    def at(x: float, y: float, z: float) -> List[float]:
        q = kin.ik((x, y, z), arm.joints())
        assert q is not None, f"{name}: ik({x}, {y}, {z}) is unreachable"
        return q

    block, cup = _scene().require("green block"), _scene().require("red cup")

    def blocked() -> List[float]:
        arm.move_joints(at(cup.x, cup.y, cup.top + 0.05), 60)
        arm.set_gripper(0)  # closed and empty: the tip may not enter the cup
        return at(cup.x, cup.y, cup.z + cup.height / 2)

    grasp_path = [at(block.x, block.y, block.top + 0.06), at(block.x, block.y, block.z + block.height / 2)]
    tol = 1e-6 if name == "fake" else 2.0
    return ArmCase(arm, at(0.20, 0.05, 0.12), grasp_path, start, blocked, CollisionError, tol, [env.close])


def _robotd_arm_case(make_arm: Callable[[str], object]) -> ArmCase:
    server = _robotd_server()
    url = server.__enter__()
    cleanup = [lambda: server.__exit__(None, None, None)]
    try:
        arm = make_arm(url)
    except BaseException:
        cleanup[0]()
        raise
    close = getattr(arm, "close", None)
    if callable(close):
        cleanup.insert(0, close)
    return ArmCase(arm, [10.0, -20.0, 30.0, 5.0, 0.0, 100.0],
                   [[GRASP_PAN + 10, -10.0, 20.0, 0.0, 0.0, 100.0]], [0.0] * 5 + [100.0],
                   lambda: [0.0, 0.0, HARD_STOP_ELBOW + 20, 0.0, 0.0, 100.0], Exception, 1.5, cleanup)


ARM_BACKENDS = {
    "fake": lambda: _scene_arm_case("fake"),
    "sim": lambda: _scene_arm_case("sim"),
    "robotd": lambda: _robotd_arm_case(_RobotdHttpArm),
    "robotd-client": lambda: _robotd_arm_case(_robotd_client),
}


@pytest.fixture(params=list(ARM_BACKENDS))
def arm_case(request) -> Iterator[ArmCase]:
    case = ARM_BACKENDS[request.param]()
    yield case
    for fn in case.cleanup:
        fn()


def _close_to(a: Sequence[float], b: Sequence[float], tol: float) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


def test_arm_has_a_name(arm_case):
    assert isinstance(arm_case.arm.name, str) and arm_case.arm.name


def test_joints_are_five_arm_joints_and_a_gripper(arm_case):
    q = arm_case.arm.joints()
    assert len(q) == 6 and all(isinstance(v, float) for v in q), q
    assert 0.0 <= q[5] <= 100.0, f"gripper must be 0..100, got {q[5]}"


def test_joints_returns_a_copy(arm_case):
    q = arm_case.arm.joints()
    q[0] += 5.0
    assert arm_case.arm.joints()[0] != q[0], "mutating joints() changed the arm's reported state"


def test_move_joints_returns_within_tolerance(arm_case):
    arm_case.arm.move_joints(arm_case.reachable, 60)
    q = arm_case.arm.joints()
    assert _close_to(q[:5], arm_case.reachable[:5], arm_case.joint_tol), (q, arm_case.reachable)


def test_move_joints_leaves_the_gripper_alone(arm_case):
    """Seeds are copied before a grasp; a move must never loosen the grip mid-carry."""
    arm_case.arm.set_gripper(70)
    target = list(arm_case.reachable)
    target[5] = 10.0
    arm_case.arm.move_joints(target, 60)
    assert abs(arm_case.arm.joints()[5] - 70.0) <= 1.5, arm_case.arm.joints()


def test_move_joints_raises_when_blocked(arm_case):
    target = arm_case.blocked()
    with pytest.raises(arm_case.blocked_exc):
        arm_case.arm.move_joints(target, 60)


def test_open_gripper_reads_open_and_not_holding(arm_case):
    arm_case.arm.set_gripper(100)
    assert abs(arm_case.arm.joints()[5] - 100.0) <= 1.5
    assert arm_case.arm.gripper_holding() is False


def test_closing_on_nothing_is_not_holding(arm_case):
    arm_case.arm.set_gripper(100)
    arm_case.arm.move_joints(arm_case.empty_pose, 60)
    arm_case.arm.set_gripper(0)
    assert arm_case.arm.gripper_holding() is False


def test_closing_on_an_object_holds_until_released(arm_case):
    arm_case.arm.set_gripper(100)
    for pose in arm_case.grasp_path:
        arm_case.arm.move_joints(pose, 60)
    arm_case.arm.set_gripper(0)
    assert arm_case.arm.gripper_holding() is True
    arm_case.arm.set_gripper(100)
    assert arm_case.arm.gripper_holding() is False


def test_stop_holds_position_and_the_arm_still_moves_after(arm_case):
    before = arm_case.arm.joints()
    arm_case.arm.stop()
    assert _close_to(arm_case.arm.joints()[:5], before[:5], arm_case.joint_tol)
    arm_case.arm.move_joints(arm_case.reachable, 60)
    assert _close_to(arm_case.arm.joints()[:5], arm_case.reachable[:5], arm_case.joint_tol)


def _status(err: BaseException) -> Optional[int]:
    """The HTTP status a robotd client exception reports (attribute or "-> 409" in the message)."""
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    if isinstance(status, int):
        return status
    found = re.search(r"\b([1-5]\d\d)\b", str(err))
    return int(found.group(1)) if found else None


STALLS = {
    # robot.yaml's step and lag (10/10): the elbow falls a whole step behind, the watchdog halts the move
    "watchdog": ({"max_step_deg": 10, "max_lag_deg": 10}, "lag"),
    # big steps never lag past the watchdog, so the arm times out short of the target instead
    "not-settled": ({"max_step_deg": 30}, "errors"),
}


@pytest.mark.parametrize("stall", sorted(STALLS))
@pytest.mark.parametrize("client", ["robotd", "robotd-client"])
def test_robotd_stall_is_a_409_naming_the_joint(stall, client):
    """INTERFACES §2: a stalled move is refused with 409 (not 400 = bad request, not 503 = bus error,
    and never a 200 that leaves the arm short), says which joint stalled, and holds the arm there."""
    ctl_kwargs, detail = STALLS[stall]
    make_arm = _RobotdHttpArm if client == "robotd" else _robotd_client
    with _robotd_server(**ctl_kwargs) as url:
        arm = make_arm(url)
        try:
            with pytest.raises(Exception) as info:
                arm.move_joints([0.0, 0.0, HARD_STOP_ELBOW + 20, 0.0, 0.0, 100.0], 60)
            assert _status(info.value) == 409, f"stall answered {info.value!r}"
            if isinstance(info.value, RobotdHttpError):
                assert info.value.body["ok"] is False and "elbow_flex" in info.value.body[detail], info.value.body
            assert abs(arm.joints()[2] - HARD_STOP_ELBOW) <= 1.5, arm.joints()
        finally:
            close = getattr(arm, "close", None)
            if callable(close):
                close()


# ---- Kinematics -----------------------------------------------------------------------------

KIN_BACKENDS = {
    "fake": lambda: _scene_env("fake").kin,
    "sim": lambda: _scene_env("sim").kin,
    "placo": _placo,
}

SEED = [0.0, 0.0, 0.0, 0.0, 0.0, 37.0]


@pytest.fixture(params=list(KIN_BACKENDS))
def kin(request):
    return KIN_BACKENDS[request.param]()


def test_fk_is_a_point(kin):
    p = kin.fk(kin.ik(WORKSPACE_POINTS[0], SEED))
    assert len(p) == 3 and all(isinstance(v, float) for v in p), p


@pytest.mark.parametrize("target", WORKSPACE_POINTS)
def test_ik_round_trips_through_fk(kin, target):
    q = kin.ik(target, SEED)
    assert q is not None, f"ik{target} unreachable"
    assert len(q) == 6
    assert math.dist(kin.fk(q), target) <= TIP_TOL_M, (kin.fk(q), target)


def test_ik_copies_the_gripper_from_the_seed(kin):
    assert kin.ik(WORKSPACE_POINTS[1], SEED)[5] == SEED[5]


def test_ik_leaves_the_seed_alone_and_is_deterministic(kin):
    seed = list(SEED)
    first = kin.ik(WORKSPACE_POINTS[2], seed)
    assert seed == SEED
    assert _close_to(kin.ik(WORKSPACE_POINTS[2], seed), first, 1e-6)


def test_ik_of_an_unreachable_point_is_none(kin):
    assert kin.ik(UNREACHABLE, SEED) is None


# ---- Perception -----------------------------------------------------------------------------

def _worldmodel():
    try:
        from skillsmith.perception.world import WorldModel

        return WorldModel(load_config(), frame_source=MockCamera("front"))
    except (NotImplementedError, ImportError) as e:
        built(e, "vision", "waiting on WS3.5 (WorldModel)")


PERCEPTION_BACKENDS = {
    "fake": lambda: _scene_env("fake").perception,
    "sim": lambda: _scene_env("sim").perception,
    "worldmodel": _worldmodel,
}


@pytest.fixture(params=list(PERCEPTION_BACKENDS))
def perception(request):
    p = PERCEPTION_BACKENDS[request.param]()
    yield p
    close = getattr(p, "close", None)
    if callable(close):
        close()


def test_observe_returns_a_scene_of_scene_objects(perception):
    scene = perception.observe()
    assert isinstance(scene, Scene)
    assert isinstance(scene.objects, tuple) and all(isinstance(o, SceneObject) for o in scene.objects)
    assert isinstance(scene.t, float) and isinstance(scene.source, str) and scene.source
    ids = [o.id for o in scene.objects]
    assert len(ids) == len(set(ids)), f"object ids must be unique: {ids}"
    for o in scene.objects:
        assert o.height > 0 and o.width > 0 and 0.0 <= o.confidence <= 1.0, o


def test_snapshot_returns_none_or_the_saved_path(perception, tmp_path):
    path = str(tmp_path / "frame.jpg")
    got = perception.snapshot(path)
    assert got is None or (got == path and Path(path).is_file()), got


def _optional(perception, method: str):
    fn = getattr(perception, method, None)
    if fn is None:
        pytest.skip(f"{type(perception).__name__} has no optional {method}()")
    try:
        return fn()
    except NotImplementedError as e:
        pytest.skip(f"waiting on {e} ({method})")


def test_unexpected_motion_is_none_or_a_message(perception):
    msg = _optional(perception, "unexpected_motion")
    assert msg is None or (isinstance(msg, str) and msg)


@pytest.mark.parametrize("method", ["perception_ok", "holding_check"])
def test_checks_answer_ok_and_why(perception, method):
    got = _optional(perception, method)
    assert isinstance(got, tuple) and len(got) == 2, got
    assert isinstance(got[0], bool) and isinstance(got[1], str), got


# ---- Env: the three parts agree with each other ---------------------------------------------

@pytest.fixture(params=["fake", "sim"])
def scene_env(request) -> Iterator[Env]:
    env = _scene_env(request.param)
    yield env
    env.close()


def test_make_env_names_the_env(scene_env, request):
    assert scene_env.name == request.node.callspec.params["scene_env"]


def test_make_env_rejects_an_unknown_backend():
    with pytest.raises(ValueError):
        make_env("bogus", load_config())


def test_observe_starts_from_the_scene_it_was_built_from(scene_env):
    want = {o.id: o for o in _scene().objects}
    got = {o.id: o for o in scene_env.perception.observe().objects}
    assert set(got) == set(want)
    for oid, o in want.items():
        assert math.dist((o.x, o.y, o.z), (got[oid].x, got[oid].y, got[oid].z)) <= SCENE_TOL_M, (oid, got[oid])


def _tip_to(env: Env, xyz: Tuple[float, float, float]) -> None:
    q = env.kin.ik(xyz, env.arm.joints())
    assert q is not None, f"ik{xyz} unreachable"
    env.arm.move_joints(q, 60)


def test_fk_of_the_arm_matches_where_it_was_sent(scene_env):
    target = WORKSPACE_POINTS[1]
    _tip_to(scene_env, target)
    assert math.dist(scene_env.kin.fk(scene_env.arm.joints()), target) <= TIP_TOL_M


def _lift_block(env: Env) -> SceneObject:
    """Grasp the green block and lift it LIFT_M; returns where it started."""
    b = _scene().require("green block")
    env.arm.set_gripper(100)
    _tip_to(env, (b.x, b.y, b.top + 0.06))
    _tip_to(env, (b.x, b.y, b.z + b.height / 2))
    env.arm.set_gripper(0)
    assert env.arm.gripper_holding()
    _tip_to(env, (b.x, b.y, b.z + b.height / 2 + LIFT_M))
    return b


def test_a_held_object_follows_the_gripper(scene_env):
    b = _lift_block(scene_env)
    lifted = scene_env.perception.observe().require(b.id)
    assert abs(lifted.z - (b.z + LIFT_M)) <= HELD_TOL_M, f"held block at z={lifted.z}, not lifted {LIFT_M} m"


def test_a_released_object_drops_onto_the_table(scene_env):
    b = _lift_block(scene_env)
    scene_env.arm.set_gripper(100)
    dropped = scene_env.perception.observe().require(b.id)
    assert math.dist((dropped.x, dropped.y, dropped.z), (b.x, b.y, b.z)) <= HELD_TOL_M, dropped
