"""Natural-language task → TaskSpec, for the demo task families.

Templates cover the demo tasks: put-in, stack, tower, sort-by-colour and move-to-a-point.
Anything else goes to the **fallback hook** if one is set (`set_fallback`, or the `fallback`
argument): a callable that turns the text into a TaskSpec, e.g. a read-only `droid exec -o json`
call (WS5 stretch). A fallback spec usually names a skill the library lacks, so the repair loop
sends it down Droid's draft path. With no fallback (or one that declines) the text needs explicit
--skill/--params/--success.

Success predicates follow docs/INTERFACES.md §5. A tower is checked as `all` of `on_top` pairs
(works with today's verify.py); sorting uses `{"op": "sorted_by_color", "args": [kind, into]}`,
the WS4.7 predicate, referenced by name only.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from skillsmith.types import TaskSpec

TaskFallback = Callable[[str], Optional[TaskSpec]]

MAX_TOWER = 6  # matches MAX_LEVELS in skills/library/tower.py
_THE = r"(?:the\s+)?"
_ON = r"\s+on(?:\s+top\s+of)?\s+"
_IN = r"\s+(?:in(?!\s+front\b)|into|inside)\s+"  # "in front of" is a place, not a container
_WORD = re.compile(r"[a-z]+")
_SKILL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LIST_SEP = re.compile(r"\s*,\s*(?:and\s+|then\s+)?|\s+(?:and|then)\s+")
_BOTTOM_FIRST = re.compile(r"\s*,?\s*\(?(?:bottom\s+(?:first|up)|from\s+the\s+bottom(?:\s+up)?)\)?$")
_BY_COLOUR = re.compile(r"\s*\bby\s+colou?rs?\b\s*")

_fallback: Optional[TaskFallback] = None


def set_fallback(fn: Optional[TaskFallback]) -> Optional[TaskFallback]:
    """Install the hook for text no template matches; returns the previous one. None removes it."""
    global _fallback
    previous, _fallback = _fallback, fn
    return previous


def get_fallback() -> Optional[TaskFallback]:
    return _fallback


def _singular(word: str) -> str:
    if re.search(r"(?:x|ch|sh|ss)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _strip_the(name: str) -> str:
    return re.sub(r"^the\s+", "", name.strip())


def _split_list(text: str) -> list[str]:
    return [_strip_the(p) for p in _LIST_SEP.split(_BOTTOM_FIRST.sub("", text)) if p.strip()]


def _tower(objs: list[str]) -> Optional[TaskSpec]:
    """objs bottom first. None (no match) for fewer than 2, too many, or repeated objects."""
    if not 2 <= len(objs) <= MAX_TOWER or len(set(objs)) != len(objs):
        return None
    levels = [{"op": "on_top", "args": [objs[i], objs[i - 1]]} for i in range(1, len(objs))]
    success = levels[0] if len(levels) == 1 else {"op": "all", "args": levels}
    return TaskSpec("", "tower", {"objs": objs}, success)


def _sort(kind: Optional[str], into: Optional[str]) -> TaskSpec:
    k, c = _singular(kind or "block"), _singular(into or "cup")
    return TaskSpec("", "sort_by_color", {"kind": k, "into": c}, {"op": "sorted_by_color", "args": [k, c]})


def _sort_phrase(rest: str) -> Optional[TaskSpec]:
    """After 'sort': '[all] [the] blocks [into the [matching] cups]', 'by colour' anywhere. Else None."""
    by_colour = bool(_BY_COLOUR.search(f" {rest} "))
    words = _BY_COLOUR.sub(" ", f" {rest} ").strip()
    parts = re.split(r"(?:^|\s+)(?:in|into)(?:\s+|$)", words, maxsplit=1)
    kind_part, into_part = parts[0], (parts[1] if len(parts) > 1 else "")
    kind = re.sub(r"^(?:all\s+)?(?:of\s+)?(?:the\s+)?", "", kind_part.strip())
    into = re.sub(r"^(?:the\s+)?(?:matching\s+)?", "", into_part.strip())
    if any(w and not _WORD.fullmatch(w) for w in (kind, into)):
        return None
    if not (kind or into or by_colour):  # bare "sort" says nothing to sort
        return None
    return _sort(kind or None, into or None)


def _put_in(a: str, b: str) -> TaskSpec:
    return TaskSpec("", "put_in", {"obj": a, "container": b}, {"op": "inside", "args": [a, b]})


def _stack(a: str, b: str) -> TaskSpec:
    return TaskSpec("", "stack", {"top": a, "bottom": b}, {"op": "on_top", "args": [a, b]})


def _pick_place(a: str, x: str, y: str) -> TaskSpec:
    fx, fy = float(x), float(y)
    return TaskSpec("", "pick_place", {"obj": a, "x": fx, "y": fy}, {"op": "near", "args": [a, fx, fy]})


_Builder = Callable[["re.Match[str]"], Optional[TaskSpec]]
_COLOUR = r"colou?rs?"

# Order matters: sort and tower phrasings come before put-in and stack, which would also match them.
# A builder returning None means "not this template"; the next one is tried.
_TEMPLATES: list[tuple[re.Pattern[str], _Builder]] = [
    # sort: "sort the blocks into the cups by colour", "sort by colour", "sort the blocks by color"
    (re.compile(r"^sort(?:\s+(?P<rest>.+))?$"), lambda m: _sort_phrase(m["rest"] or "")),
    # sort: "put each block in the cup of the same colour", "put every block into the matching cup"
    (re.compile(rf"^(?:put|place|drop|move)\s+(?:each|every|all)\s+(?:of\s+)?{_THE}(?P<kind>[a-z]+)\s+"
                rf"(?:in|into)\s+{_THE}(?:(?:matching|same[\s-]{_COLOUR}(?:ed)?)\s+(?P<a>[a-z]+)"
                rf"|(?P<b>[a-z]+)\s+(?:of|with)\s+{_THE}same\s+{_COLOUR}"
                rf"|(?P<c>[a-z]+)\s+(?:that\s+)?match(?:es|ing)?\s+(?:its|their)\s+{_COLOUR})$"),
     lambda m: _sort(m["kind"], m["a"] or m["b"] or m["c"])),
    # tower: "build a tower of the red block, the green block and the blue block" (bottom first)
    (re.compile(r"^(?:build|make|stack|create)\s+(?:a\s+)?tower\s+(?:of|with|from|out\s+of|using)\s+(?P<objs>.+)$"),
     lambda m: _tower(_split_list(m["objs"]))),
    # tower: "stack the red block, the green block and the blue block into a tower"
    (re.compile(r"^(?:stack|build|make|pile)\s+(?:up\s+)?(?P<objs>.+?)\s+(?:in)?to\s+a\s+tower$"),
     lambda m: _tower(_split_list(m["objs"]))),
    # tower: "stack the blue block on the green block on the red block" (top first in the text)
    (re.compile(rf"^(?:stack|put|place)\s+(?P<objs>.+?{_ON}.+?{_ON}.+)$"),
     lambda m: _tower([_strip_the(p) for p in re.split(_ON, m["objs"])][::-1])),
    # put-in: "put the red block in the green cup", "drop the block into the bowl"
    (re.compile(rf"^(?:put|place|drop|move|insert)\s+{_THE}(?P<a>.+?){_IN}{_THE}(?P<b>.+)$"),
     lambda m: _put_in(m["a"], m["b"])),
    # stack: "stack the blue cup on the red cup"
    (re.compile(rf"^(?:stack|put|place)\s+{_THE}(?P<a>.+?){_ON}{_THE}(?P<b>.+)$"),
     lambda m: _stack(m["a"], m["b"])),
    # pick-place: "move the red block to 0.2, -0.1"
    (re.compile(rf"^move\s+{_THE}(?P<a>.+?)\s+to\s+(?P<x>-?[\d.]+)\s*,\s*(?P<y>-?[\d.]+)$"),
     lambda m: _pick_place(m["a"], m["x"], m["y"])),
]


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip(".!")


def match_template(text: str) -> Optional[TaskSpec]:
    """The TaskSpec from the first matching template, or None. Never calls the fallback."""
    clean = normalise(text)
    for pattern, build in _TEMPLATES:
        m = pattern.match(clean)
        spec = build(m) if m else None
        if spec is not None:
            spec.text = text.strip()
            return spec
    return None


def spec_from_dict(text: str, data: object) -> TaskSpec:
    """Check a fallback's answer (a TaskSpec or a JSON-style dict) and return a clean TaskSpec.

    The skill name becomes a library file name, so it must be a plain lower_snake_case identifier.
    """
    if isinstance(data, TaskSpec):
        data = {"skill": data.skill, "params": data.params, "success": data.success}
    if not isinstance(data, dict):
        raise ValueError(f"task fallback returned {type(data).__name__}, expected a TaskSpec or dict")
    skill, params, success = data.get("skill"), data.get("params", {}), data.get("success")
    if not isinstance(skill, str) or not _SKILL_NAME.fullmatch(skill):
        raise ValueError(f"task fallback gave skill name {skill!r}; use lower_snake_case letters and digits")
    if not isinstance(params, dict):
        raise ValueError(f"task fallback gave params {params!r}; expected a dict")
    if not isinstance(success, dict) or not isinstance(success.get("op"), str):
        raise ValueError(f"task fallback gave success {success!r}; expected a predicate dict with 'op'")
    return TaskSpec(text.strip(), skill, dict(params), dict(success))


def parse_task(text: str, fallback: Optional[TaskFallback] = None) -> TaskSpec:
    """Templates first, then `fallback` (or the hook from `set_fallback`). ValueError if neither gives a spec."""
    spec = match_template(text)
    if spec is not None:
        return spec
    hook = fallback or _fallback
    if hook is not None:
        try:
            answer = hook(text)
        except Exception as e:  # a broken hook surfaces as a parse error, which callers report as an event
            raise ValueError(f"can't parse task '{text}': fallback failed ({type(e).__name__}: {e})") from e
        if answer is not None:
            return spec_from_dict(text, answer)
    raise ValueError(f"can't parse task '{text}'; pass --skill, --params and --success explicitly")
