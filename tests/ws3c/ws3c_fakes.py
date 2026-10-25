"""WS3c fakes: synthetic detections whose point_uv is already the robot-frame (x, y) of the object's
top, a camera model that passes it through, a frame source, and an arm + FK (stdlib only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from skillsmith.config import Config, load_config
from skillsmith.perception.frames import Frame, FrameError
from skillsmith.perception.world import WorldModel


@dataclass(frozen=True)
class Det:
    label: str
    color: str
    kind: str
    point_uv: Tuple[float, float]
    confidence: float = 0.9
    bbox: Tuple[int, int, int, int] = (0, 0, 1, 1)


def cup(color: str, x: float, y: float, confidence: float = 0.9) -> Det:
    return Det(f"{color} cup", color, "cup", (x, y), confidence)


class PassCamera:
    """pixel == robot-frame (x, y): positions are already in the robot frame."""

    def __init__(self, drift: float = 0.0):
        self.planes: List[float] = []
        self.drift = drift  # metres reported by marker_drift; tests move the camera by setting it
        self.drift_checks = 0

    def pixel_to_table(self, u: float, v: float, z_plane: float) -> Tuple[float, float]:
        self.planes.append(z_plane)
        return (u, v)

    def marker_drift(self, _frame) -> float:
        self.drift_checks += 1
        return self.drift


class FakeFrames:
    def __init__(self):
        self.n = 0
        self.fail: Optional[str] = None
        self.closed = False

    def read(self) -> Frame:
        if self.fail:
            raise FrameError(self.fail)
        self.n += 1
        return Frame(b"\xff\xd8" + f"f{self.n}".encode() + b"\xff\xd9", 1000.0 + self.n, "robotd:front")

    def close(self) -> None:
        self.closed = True


class FakeDetector:
    """Returns `dets` every frame (tests change it), or calls `fn()` when set."""

    def __init__(self, dets: Sequence[Det] = (), fn=None):
        self.dets = list(dets)
        self.fn = fn

    def detect(self, _image) -> List[Det]:
        return list(self.fn()) if self.fn else list(self.dets)


class FakeArm:
    """joints[:3] is the tip position; FakeKin.fk returns it."""

    def __init__(self, tip=(0.2, 0.0, 0.2)):
        self.q = [tip[0], tip[1], tip[2], 0.0, 0.0, 100.0]
        self.holding = False

    def joints(self) -> List[float]:
        return list(self.q)

    def gripper_holding(self) -> bool:
        return self.holding

    def move_tip(self, x: float, y: float, z: float) -> None:
        self.q[0:3] = [x, y, z]


class FakeKin:
    def fk(self, joints: Sequence[float]):
        return (joints[0], joints[1], joints[2])


def make_world(dets=(), arm=None, bus=None, cfg=None, **kw):
    cfg = cfg or _fixture_config()
    detector = kw.pop("detector", None) or FakeDetector(dets)
    frames = kw.pop("frames", None) or FakeFrames()
    camera = kw.pop("camera", None) or PassCamera()
    return WorldModel(cfg, frames, kinematics=FakeKin() if arm else None, arm=arm, detector=detector,
                      camera=camera, bus=bus, decode=lambda f: f, **kw)


def _fixture_config():
    """robot.yaml without the rig's measured objects and detector: the recorded fixtures use 8 cm cups."""
    data = load_config().raw()
    data = {**data, "world": {k: v for k, v in (data.get("world") or {}).items() if k != "objects"}}
    data.pop("detect", None)
    return Config(data)
