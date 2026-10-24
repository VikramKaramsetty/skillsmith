"""Natural-language task → TaskSpec, for the demo task families.

Templates cover the planned demo tasks. Anything else needs explicit --skill/--params/
--success, or (WS5 stretch) a read-only `droid exec -o json` call that returns a TaskSpec.
"""

from __future__ import annotations

import re

from skillsmith.types import TaskSpec

_THE = r"(?:the\s+)?"
_TEMPLATES = [
    (re.compile(rf"^(?:put|place|drop)\s+{_THE}(?P<a>.+?)\s+(?:in|into|inside)\s+{_THE}(?P<b>.+)$"),
     lambda m: TaskSpec("", "put_in", {"obj": m["a"], "container": m["b"]},
                        {"op": "inside", "args": [m["a"], m["b"]]})),
    (re.compile(rf"^(?:stack|put|place)\s+{_THE}(?P<a>.+?)\s+on(?:\s+top\s+of)?\s+{_THE}(?P<b>.+)$"),
     lambda m: TaskSpec("", "stack", {"top": m["a"], "bottom": m["b"]},
                        {"op": "on_top", "args": [m["a"], m["b"]]})),
    (re.compile(rf"^move\s+{_THE}(?P<a>.+?)\s+to\s+(?P<x>-?[\d.]+)\s*,\s*(?P<y>-?[\d.]+)$"),
     lambda m: TaskSpec("", "pick_place", {"obj": m["a"], "x": float(m["x"]), "y": float(m["y"])},
                        {"op": "near", "args": [m["a"], float(m["x"]), float(m["y"])]})),
]


def parse_task(text: str) -> TaskSpec:
    clean = re.sub(r"\s+", " ", text.strip().lower()).rstrip(".!")
    for pattern, build in _TEMPLATES:
        m = pattern.match(clean)
        if m:
            spec = build(m)
            spec.text = text.strip()
            return spec
    raise ValueError(f"can't parse task '{text}'; pass --skill, --params and --success explicitly")
