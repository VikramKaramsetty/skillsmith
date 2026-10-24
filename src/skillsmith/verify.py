"""Success predicates over a Scene. See docs/INTERFACES.md §5.

`detail` strings go straight into Droid's failure report: make them specific and numeric.

Ops: on_top, inside, near, all, any, not (§5), plus (WS4.7):
  {"op": "tower", "args": ["red block", "green block", "blue block"]}   # bottom first
  {"op": "sorted_by_color", "args": ["block", "cup"]}                   # each block in its colour's cup
  {"op": "left_of", "args": ["red block", "green block", 0.02]}         # +y is left; margin optional
  {"op": "upright", "args": ["red cup", 0.08]}                          # expected height optional
"""

from __future__ import annotations

import math

from skillsmith.types import Scene, VerifyResult


def evaluate(pred: dict, scene: Scene, xy_tol: float = 0.03, z_tol: float = 0.01) -> VerifyResult:
    op, args = pred.get("op"), pred.get("args", [])
    fn = _OPS.get(op)
    if fn is None:
        return VerifyResult(False, f"unknown predicate op '{op}' (known: {', '.join(sorted(_OPS))})")
    try:
        return fn(scene, args, xy_tol, z_tol)
    except (TypeError, ValueError, IndexError, KeyError) as e:
        return VerifyResult(False, f"bad args for '{op}': {args!r} ({type(e).__name__}: {e})")


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


def _cm(metres: float) -> str:
    return f"{metres * 100:.1f} cm"


def _tower(scene, args, xy_tol, z_tol):
    """args: names bottom first, e.g. ["red block", "green block", "blue block"] (or one nested list)."""
    names = list(args[0]) if len(args) == 1 and isinstance(args[0], (list, tuple)) else list(args)
    if len(names) < 2:
        return VerifyResult(False, f"tower needs at least 2 objects, got {len(names)}: {names}")
    miss = _missing(scene, *names)
    if miss:
        return miss
    objs = [scene.find(n) for n in names]
    seen: dict = {}
    for n, o in zip(names, objs):
        if o.id in seen:
            return VerifyResult(False, f"tower lists '{seen[o.id]}' and '{n}', but both are {o.label}")
        seen[o.id] = n
    order = " / ".join(o.label for o in objs)
    broken, worst = [], 0.0
    for i in range(1, len(objs)):
        top, below = objs[i], objs[i - 1]
        d = top.xy_dist(below)
        worst = max(worst, d)
        if d > xy_tol:
            broken.append(f"level {i + 1}: {top.label} is {_cm(d)} from {below.label} in xy (limit {_cm(xy_tol)})")
        elif top.z < below.top - z_tol:
            broken.append(f"level {i + 1}: {top.label} base z={top.z:.3f} is below {below.label} "
                          f"top z={below.top:.3f} (it is not resting on it)")
    if broken:
        return VerifyResult(False, f"tower of {len(objs)} ({order}, bottom first) is broken at "
                                   + "; ".join(broken))
    return VerifyResult(True, f"tower of {len(objs)} stands ({order}, bottom first); "
                              f"top at z={objs[-1].top:.3f}, worst xy offset {_cm(worst)}")


def _sorted_by_color(scene, args, xy_tol, z_tol):
    """args: [kind, into], default ["block", "cup"]: every <kind> sits in the <into> of its colour.

    "In" means centred within the container's opening (xy < its radius) and not below its base, so
    an object resting on the rim counts (the fake world's cups are solid); use `inside` for depth.
    """
    if isinstance(args, dict):
        kind, into = args.get("kind", "block"), args.get("into", "cup")
    else:
        kind, into = (list(args) + ["block", "cup"][len(args):])[:2]
    items = [o for o in scene.objects if o.kind == kind]
    bins = [o for o in scene.objects if o.kind == into]
    seen = ", ".join(o.label for o in scene.objects) or "nothing"
    if not items:
        return VerifyResult(False, f"no {kind}s to check (saw: {seen})")
    if not bins:
        return VerifyResult(False, f"no {into}s to sort {kind}s into (saw: {seen})")
    wrong = []
    for o in items:
        match = [b for b in bins if b.color == o.color]
        if not match:
            wrong.append(f"no {o.color} {into} for {o.label}")
            continue
        target = min(match, key=o.xy_dist)
        if _in_opening(o, target, z_tol):
            continue
        other = next((b for b in bins if b is not target and _in_opening(o, b, z_tol)), None)
        if other is not None:
            wrong.append(f"{o.label} is in {other.label}, not {target.label}")
        else:
            wrong.append(f"{o.label} is {_cm(o.xy_dist(target))} from {target.label} centre "
                         f"(must be < {_cm(target.width / 2)})")
    if wrong:
        return VerifyResult(False, f"{len(wrong)} of {len(items)} {kind}s not sorted by colour: " + "; ".join(wrong))
    return VerifyResult(True, f"all {len(items)} {kind}s are in the {into} of their colour")


def _in_opening(obj, container, z_tol) -> bool:
    return obj.xy_dist(container) < container.width / 2 and obj.z >= container.z - z_tol


def _left_of(scene, args, xy_tol, z_tol):
    """args: [a, b] or [a, b, margin_m]. Left = +y in the robot base frame (the robot's left)."""
    a_name, b_name = args[0], args[1]
    margin = float(args[2]) if len(args) > 2 else 0.0
    miss = _missing(scene, a_name, b_name)
    if miss:
        return miss
    a, b = scene.find(a_name), scene.find(b_name)
    dy = a.y - b.y
    pos = f"(y={a.y:.3f} vs y={b.y:.3f}; +y is the robot's left)"
    if dy > margin:
        return VerifyResult(True, f"{a.label} is {_cm(dy)} left of {b.label} {pos}")
    side = "right of" if dy < 0 else "level with"
    need = f"more than {_cm(margin)} left" if margin > 0 else "left"
    return VerifyResult(False, f"{a.label} is {_cm(abs(dy))} {side} {b.label} {pos}; it must be {need}")


def _upright(scene, args, xy_tol, z_tol):
    """args: [name] or [name, expected_height_m].

    The scene carries no orientation, so this reads the measured height: with an expected height it
    must be within z_tol of it; without one the object must be at least as tall as it is wide (a cup
    lying on its side reads wider than tall). A cube always passes the second form.
    """
    name = args[0]
    expected = float(args[1]) if len(args) > 1 else None
    miss = _missing(scene, name)
    if miss:
        return miss
    o = scene.find(name)
    if not o.visible:
        return VerifyResult(False, f"{o.label} is hidden (last seen at ({o.x:.2f}, {o.y:.2f})); cannot tell if upright")
    if expected is not None:
        if o.height < expected - z_tol:
            return VerifyResult(False, f"{o.label} looks tipped over: height {_cm(o.height)}, upright it is "
                                       f"{_cm(expected)} (tolerance {_cm(z_tol)})")
        return VerifyResult(True, f"{o.label} is upright (height {_cm(o.height)}, expected {_cm(expected)})")
    if o.height < o.width - z_tol:
        return VerifyResult(False, f"{o.label} looks tipped over: height {_cm(o.height)} is less than its "
                                   f"width {_cm(o.width)} (tolerance {_cm(z_tol)})")
    return VerifyResult(True, f"{o.label} is upright (height {_cm(o.height)}, width {_cm(o.width)})")


_OPS = {
    "on_top": _on_top, "inside": _inside, "near": _near, "all": _all, "any": _any, "not": _not,
    "tower": _tower, "sorted_by_color": _sorted_by_color, "left_of": _left_of, "upright": _upright,
}
