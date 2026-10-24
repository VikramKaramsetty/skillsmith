"""robotd HTTP API (docs/INTERFACES.md §2). OWNER: WS1.

`create_app(controller, cameras, token=None)` builds the FastAPI app. FastAPI is imported lazily
so this module imports with core deps only. Endpoints are plain `def`, so FastAPI runs each one in
its threadpool: `/stop` and `/state` answer while a blocking `/move_joints` is still running.
Errors answer `{"ok": false, "error": "...", ...details}` with 400 (bad request), 401 (token),
404 (unknown camera) or 409 (motion refused, stopped or not settled).
With a token set, every request must carry it in the `X-Robotd-Token` header.
No `from __future__ import annotations` here: FastAPI must resolve the locally defined body models.
"""

import hmac
import time
from typing import Dict, Iterator, List, Optional, Protocol

from robotd.controller import ArmController, RobotdError

TOKEN_HEADER = "X-Robotd-Token"
STREAM_FPS = 15.0
BOUNDARY = "frame"


class Camera(Protocol):
    def jpeg(self) -> bytes: ...


def mjpeg(camera: Camera, fps: float = STREAM_FPS, max_frames: Optional[int] = None) -> Iterator[bytes]:
    """multipart/x-mixed-replace parts, one JPEG each. Endless unless max_frames is set."""
    sent = 0
    while max_frames is None or sent < max_frames:
        if sent:
            time.sleep(1.0 / fps)
        frame = camera.jpeg()
        head = f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
        yield head.encode() + frame + b"\r\n"
        sent += 1


def create_app(controller: ArmController, cameras: Dict[str, Camera], token: Optional[str] = None,
               stream_fps: float = STREAM_FPS, stream_max_frames: Optional[int] = None):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from pydantic import BaseModel

    class MoveBody(BaseModel):
        target: List[float]
        speed_dps: float

    class GripperBody(BaseModel):
        pct: float

    class TorqueBody(BaseModel):
        on: bool

    app = FastAPI(title="robotd")

    @app.exception_handler(RobotdError)
    async def robotd_error(_request: Request, exc: RobotdError):
        return JSONResponse(exc.payload(), status_code=exc.status)

    if token:
        @app.middleware("http")
        async def require_token(request: Request, call_next):
            if not hmac.compare_digest(request.headers.get(TOKEN_HEADER, "").encode(), token.encode()):
                return JSONResponse({"ok": False, "error": f"missing or wrong {TOKEN_HEADER} header"}, status_code=401)
            return await call_next(request)

    def camera(cam: str) -> Camera:
        if cam not in cameras:
            raise RobotdError(404, f"no camera '{cam}' (have: {', '.join(sorted(cameras)) or 'none'})")
        return cameras[cam]

    @app.get("/state")
    def state() -> dict:
        return controller.state()

    @app.post("/move_joints")
    def move_joints(body: MoveBody) -> dict:
        controller.move_joints(body.target, body.speed_dps)
        return {"ok": True}

    @app.post("/gripper")
    def gripper(body: GripperBody) -> dict:
        return {"ok": True, "holding": controller.set_gripper(body.pct)}

    @app.post("/stop")
    def stop() -> dict:
        controller.stop()
        return {"ok": True}

    @app.post("/torque")
    def torque(body: TorqueBody) -> dict:
        controller.set_torque(body.on)
        return {"ok": True, "torque": body.on}

    @app.get("/frame/{cam}")
    def frame(cam: str):
        return Response(camera(cam).jpeg(), media_type="image/jpeg")

    @app.get("/stream/{cam}")
    def stream(cam: str):
        return StreamingResponse(mjpeg(camera(cam), stream_fps, stream_max_frames),
                                 media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

    return app
