"""Success predicates over a Scene. See docs/INTERFACES.md §5.

`detail` strings go straight into Droid's failure report: make them specific and numeric.
"""

from __future__ import annotations

import math

from skillsmith.types import Scene, VerifyResult


def evaluate(pred: dict, scene: Scene, xy_tol: float = 0.03, z_tol: float = 0.01) -> VerifyResult:
    op, args = pred["op"], pred.get("args", [])
    fn = _OPS.get(op)
    if fn is None:
        return VerifyResult(False, f"unknown predicate op '{op}'")
    return fn(scene, args, xy_tol, z_tol)


def _missing(scene: Scene, *names: str):
    for n in names:
        if scene.find(n) is None:
            seen = ", ".join(o.label for o in scene.objects) or "nothing"
            return VerifyResult(False, f"cannot see '{n}' (saw: {seen})")
    return None


def _on_top(scene, args, xy_tol, z_tol):
    a_name, b_name = args
    miss = _missing(scene, a_name, b_name)
    if miss:
        return miss
    a, b = scene.find(a_name), scene.find(b_name)
    d = a.xy_dist(b)
    if d > xy_tol:
        return VerifyResult(False, f"{a.label} is {d*100:.1f} cm from {b.label} in xy "
                                   f"(limit {xy_tol*100:.1f} cm)")
    if a.z < b.top - z_tol:
        return VerifyResult(False, f"{a.label} base is at z={a.z:.3f}, below {b.label} top z={b.top:.3f}")
    return VerifyResult(True, f"{a.label} is on {b.label} (xy offset {d*100:.1f} cm)")


def _inside(scene, args, xy_tol, z_tol):
    a_name, c_name = args
    miss = _missing(scene, a_name, c_name)
    if miss:
        return miss
    a, c = scene.find(a_name), scene.find(c_name)
    d = a.xy_dist(c)
    if d > c.width / 2:
        return VerifyResult(False, f"{a.label} is {d*100:.1f} cm from {c.label} centre "
                                   f"(must be < {c.width/2*100:.1f} cm to be inside)")
    if a.z >= c.top:
        return VerifyResult(False, f"{a.label} is resting on top of {c.label}, not inside it")
    return VerifyResult(True, f"{a.label} is inside {c.label}")


def _near(scene, args, xy_tol, z_tol):
    name, x, y = args
    miss = _missing(scene, name)
    if miss:
        return miss
    a = scene.find(name)
    d = math.hypot(a.x - x, a.y - y)
    if d > xy_tol:
        return VerifyResult(False, f"{a.label} is {d*100:.1f} cm from ({x:.2f}, {y:.2f})")
    return VerifyResult(True, f"{a.label} is at ({a.x:.2f}, {a.y:.2f})")


def _all(scene, args, xy_tol, z_tol):
    results = [evaluate(p, scene, xy_tol, z_tol) for p in args]
    failed = [r.detail for r in results if not r.ok]
    return VerifyResult(not failed, "; ".join(failed) if failed else "all conditions met")


def _any(scene, args, xy_tol, z_tol):
    results = [evaluate(p, scene, xy_tol, z_tol) for p in args]
    ok = [r.detail for r in results if r.ok]
    return VerifyResult(bool(ok), ok[0] if ok else "; ".join(r.detail for r in results))


def _not(scene, args, xy_tol, z_tol):
    r = evaluate(args[0], scene, xy_tol, z_tol)
    return VerifyResult(not r.ok, f"NOT({r.detail})")


_OPS = {"on_top": _on_top, "inside": _inside, "near": _near, "all": _all, "any": _any, "not": _not}
