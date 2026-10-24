"""Static checks on Droid-written skill code. See docs/INTERFACES.md §4.

This is the first gate, not a security boundary on its own: the runtime also strips
builtins, the SafetyGuard checks every target, and robotd clamps joints. It is an allow-list:
only names the runtime provides, only the skill API on the handles, and every rejection says
what to write instead.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

ROBOT_API = {"move_to", "grasp", "release", "look", "home", "position", "holding", "note", "align_over"}
SCENE_API = {"find", "require", "objects"}
SKILLS_API = {"call"}
HANDLES: Dict[str, set] = {"robot": ROBOT_API, "scene": SCENE_API, "skills": SKILLS_API}

# The only builtins a skill gets at runtime (runtime.SAFE_BUILTINS is built from this).
ALLOWED_BUILTINS = (
    "abs", "min", "max", "round", "len", "range", "float", "int", "str", "bool",
    "list", "dict", "tuple", "enumerate", "zip", "sorted", "sum", "any", "all",
    "ValueError", "RuntimeError",
)
SKILL_GLOBALS = {"math", "SkillError"}

BANNED_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "input", "breakpoint", "type", "object", "super",
    "memoryview", "help", "dir", "id", "hasattr",
}

_BANNED_NODES = {
    ast.Import: "imports are not allowed (math is already available)",
    ast.ImportFrom: "imports are not allowed (math is already available)",
    ast.While: "while loops are not allowed; use `for i in range(n)` with n <= 50",
    ast.Try: "try/except is not allowed; let errors propagate so the failure report sees them",
    ast.With: "with-statements are not allowed",
    ast.ClassDef: "classes are not allowed; write plain functions",
    ast.Global: "global is not allowed",
    ast.Nonlocal: "nonlocal is not allowed",
    ast.AsyncFunctionDef: "async is not allowed",
    ast.Await: "async is not allowed",
    ast.Yield: "generators are not allowed",
    ast.YieldFrom: "generators are not allowed",
    ast.Lambda: "lambda is not allowed; define a named function",
    ast.Delete: "del is not allowed",
}
if hasattr(ast, "TryStar"):
    _BANNED_NODES[ast.TryStar] = _BANNED_NODES[ast.Try]

# Generator, coroutine, frame, traceback and code attributes reach the runtime's frames
# (and its real builtins) without any underscore, e.g. `g.gi_frame.f_back.f_globals`.
_INTROSPECTION_PREFIXES = ("gi_", "cr_", "ag_", "f_", "tb_", "co_")
_BANNED_ATTRS = {
    "format": "str.format can read private attributes; use an f-string instead",
    "format_map": "str.format_map can read private attributes; use an f-string instead",
    "mro": "class introspection is not allowed; use only the skill API",
}
_MUTATORS = {"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
             "update", "setdefault", "popitem", "add", "discard", "difference_update",
             "intersection_update", "symmetric_difference_update"}
# Calls whose result size is set by an argument; one C-level step a timeout cannot stop.
_BIG_MATH = {"factorial", "comb", "perm"}
_SIZED_METHODS = {"ljust": "width", "rjust": "width", "center": "width", "zfill": "width",
                  "expandtabs": "tabsize", "to_bytes": "length"}
_SEQ_CALLS = {"list", "str", "tuple", "sorted"}
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

MAX_EXPONENT = 10
MAX_REPEAT = 1000
MAX_SIZE_ARG = 1000  # factorial/comb/perm arguments, padding widths, f-string widths
MAX_INT_BITS = 256  # a folded constant larger than this is rejected, never computed


class _Huge(float):
    """A folded constant too large to compute. It compares like +infinity."""


_HUGE = _Huge("inf")


class _Consts(dict):
    """Top-level constants, plus a memo of folded nodes so folding stays linear in the source."""

    def __init__(self) -> None:
        super().__init__()
        self.memo: dict = {}


@dataclass
class Issue:
    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


def validate(source: str, max_lines: int = 200, max_range: int = 50) -> List[Issue]:
    issues: List[Issue] = []
    n_lines = source.count("\n") + 1
    if n_lines > max_lines:
        issues.append(Issue(0, f"skill is {n_lines} lines; limit is {max_lines}"))
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return issues + [Issue(e.lineno or 0, f"syntax error: {e.msg}")]

    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    consts = _top_level_constants(tree)
    seqs = _sequence_names(tree)
    known = _bound_names(tree) | set(ALLOWED_BUILTINS) | SKILL_GLOBALS | set(HANDLES)
    unknown_seen: Set[str] = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        for kind, msg in _BANNED_NODES.items():
            if isinstance(node, kind):
                issues.append(Issue(line, msg))
        if isinstance(node, ast.Name):
            issues += _check_name(node, parents, funcs, line)
            if isinstance(node.ctx, ast.Load) and node.id not in known and node.id not in unknown_seen:
                unknown_seen.add(node.id)
                issues += _check_unknown(node.id, line)
        elif isinstance(node, ast.Attribute):
            issues += _check_attribute(node, parents.get(node), line)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if _root_name(node) in HANDLES:
                issues.append(Issue(line, f"{_root_name(node)} data is read-only; copy it first, e.g. "
                                          "objs = list(scene.objects)"))
        elif isinstance(node, ast.Call):
            issues += _check_range(node, parents.get(node), max_range, consts, line)
            issues += _check_sized_call(node, consts, line)
        elif isinstance(node, (ast.BinOp, ast.AugAssign)):
            issues += _check_arith(node, consts, seqs, line)
        elif isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            issues += _check_format_spec(node.format_spec, line)
        elif isinstance(node, ast.FunctionDef) and node.decorator_list:
            issues.append(Issue(line, "decorators are not allowed; call helper functions directly"))
    issues += _check_top_level(tree, consts)
    issues += _check_recursion(funcs)
    issues += _check_loop_growth(tree, consts, seqs, funcs)
    issues += _check_aliases(tree, funcs)
    return sorted(issues, key=lambda i: i.line)


def _check_name(node: ast.Name, parents: dict, funcs: Dict[str, ast.FunctionDef], line: int) -> List[Issue]:
    if node.id in BANNED_NAMES or node.id.startswith("__"):
        return [Issue(line, f"'{node.id}' is not allowed; use only the skill API and plain Python")]
    if node.id not in HANDLES:
        return []
    if isinstance(node.ctx, (ast.Store, ast.Del)):
        return [Issue(line, f"do not reassign '{node.id}'; use a different variable name")]
    parent = parents.get(node)
    if isinstance(parent, ast.Attribute) and parent.value is node:
        if parent.attr not in HANDLES[node.id]:
            allowed = ", ".join(sorted(HANDLES[node.id]))
            return [Issue(line, f"{node.id}.{parent.attr} is not in the API (allowed: {allowed})")]
        return []
    if isinstance(parent, ast.Call) and node in parent.args:
        if _param_name(parent, parent.args.index(node), funcs) == node.id:
            return []
    elif isinstance(parent, ast.keyword):
        call = parents.get(parent)
        if parent.arg == node.id and isinstance(call, ast.Call) and _param_name(call, node.id, funcs) == node.id:
            return []
    else:
        return [Issue(line, f"use '{node.id}' only as {node.id}.<method> or as a call argument")]
    return [Issue(line, f"pass '{node.id}' only to a parameter named '{node.id}' of a function in this "
                        f"skill, e.g. def helper({node.id}, ...)")]


def _param_name(call: ast.Call, slot, funcs: Dict[str, ast.FunctionDef]) -> Optional[str]:
    """The parameter of a function defined in this skill that receives argument `slot` (index or keyword)."""
    if not (isinstance(call.func, ast.Name) and call.func.id in funcs):
        return None
    args = funcs[call.func.id].args
    if isinstance(slot, int):
        params = [*args.posonlyargs, *args.args]
        return params[slot].arg if slot < len(params) else None
    return slot if slot in {a.arg for a in [*args.args, *args.kwonlyargs]} else None


def _check_unknown(name: str, line: int) -> List[Issue]:
    if name in BANNED_NAMES or name.startswith("__"):
        return []  # already reported by _check_name
    if name == "print":
        return [Issue(line, "print is not available; use robot.note(text) to show a message")]
    return [Issue(line, f"name '{name}' is not available in skills; use math, SkillError or the builtins "
                        f"{', '.join(ALLOWED_BUILTINS)}")]


def _check_attribute(node: ast.Attribute, parent, line: int) -> List[Issue]:
    attr = node.attr
    if attr.startswith("_"):
        return [Issue(line, f"attribute '{attr}' is private; use only the public skill API")]
    if attr.startswith(_INTROSPECTION_PREFIXES):
        return [Issue(line, f"attribute '{attr}' is runtime introspection; use only the skill API")]
    if attr in _BANNED_ATTRS:
        return [Issue(line, f"'.{attr}' is not allowed: {_BANNED_ATTRS[attr]}")]
    if isinstance(node.ctx, ast.Store):
        return [Issue(line, f"do not assign to attributes ('.{attr} = ...'); keep values in local variables")]
    called = isinstance(parent, ast.Call) and parent.func is node
    is_math = isinstance(node.value, ast.Name) and node.value.id == "math"
    if not called and (attr in _SIZED_METHODS or (is_math and attr in _BIG_MATH)):
        return [Issue(line, f"call .{attr}(...) directly with a constant argument; do not store or pass it")]
    root = _root_name(node.value)
    if attr in _MUTATORS and root in HANDLES and called:
        return [Issue(line, f"{root} data is read-only ('.{attr}()'); copy it first, e.g. "
                            "objs = list(scene.objects)")]
    return []


def _root_name(node: ast.AST) -> Optional[str]:
    """`scene` for `scene.objects[0].label`, `scene.require('x').z` and the like."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _check_range(node: ast.Call, parent, max_range: int, consts: dict, line: int) -> List[Issue]:
    if not (isinstance(node.func, ast.Name) and node.func.id == "range"):
        return []
    values = [_fold(arg, consts) for arg in node.args]
    for value in values:
        if isinstance(value, (int, float)) and value > max_range:
            shown = "too large" if isinstance(value, _Huge) else value
            return [Issue(line, f"range bound {shown} > {max_range}; loop over at most {max_range} items")]
    drives_loop = isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is node
    if not drives_loop and any(v is None for v in values):
        return [Issue(line, "range() with a computed bound may only drive a for loop or comprehension "
                            "(list(range(n)) or sum(range(n)) is one step a timeout cannot stop); "
                            "write `for i in range(n):` or [f(i) for i in range(n)]")]
    return []


def _check_sized_call(node: ast.Call, consts: dict, line: int) -> List[Issue]:
    """math.factorial/comb/perm and str padding: the argument sets the size of the result."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return []
    if isinstance(func.value, ast.Name) and func.value.id == "math" and func.attr in _BIG_MATH:
        shown, args = f"math.{func.attr}", [*node.args, *(k.value for k in node.keywords)]
    elif func.attr in _SIZED_METHODS:
        size = _SIZED_METHODS[func.attr]
        shown, args = f".{func.attr}", node.args[:1] + [k.value for k in node.keywords if k.arg == size]
    else:
        return []
    for arg in args:
        value = _fold(arg, consts)
        if not (isinstance(value, int) and abs(value) <= MAX_SIZE_ARG):
            return [Issue(line, f"{shown}() needs a constant argument <= {MAX_SIZE_ARG} (the result grows "
                                "faster than a timeout can stop it); use a small number literal")]
    return []


def _check_format_spec(spec: ast.AST, line: int) -> List[Issue]:
    """f'{x:>{n}}' or f'{x:>1000000000}' builds a huge string in one step."""
    if not isinstance(spec, ast.JoinedStr):
        return []
    if any(isinstance(part, ast.FormattedValue) for part in spec.values):
        return [Issue(line, "f-string widths and precisions must be constants, e.g. f'{x:.3f}' or f'{x:>8}'")]
    text = "".join(str(p.value) for p in spec.values if isinstance(p, ast.Constant))
    if any(int(digits) > MAX_SIZE_ARG for digits in re.findall(r"\d+", text)):
        return [Issue(line, f"f-string width or precision over {MAX_SIZE_ARG}; use a small one, e.g. f'{{x:.3f}}'")]
    return []


def _check_arith(node, consts: dict, seqs: Set[str], line: int) -> List[Issue]:
    """Operations a wall-clock timeout cannot interrupt: huge powers, shifts and repetitions."""
    left = node.left if isinstance(node, ast.BinOp) else node.target
    right = node.right if isinstance(node, ast.BinOp) else node.value
    if isinstance(node.op, ast.LShift):
        return [Issue(line, "bit shifts are not allowed; use plain arithmetic")]
    if isinstance(node.op, ast.Pow):
        exp = _fold(right, consts)
        if not (isinstance(exp, (int, float)) and abs(exp) <= MAX_EXPONENT):
            return [Issue(line, f"exponent must be a number with |e| <= {MAX_EXPONENT}; "
                                "use math.pow(x, y) or math.sqrt(x) otherwise")]
    if isinstance(node.op, ast.Mod) and _is_sequence(left, seqs):
        return [Issue(line, "%-formatting is not allowed; use an f-string instead")]
    if isinstance(node.op, ast.Mult):
        for seq, count in ((left, right), (right, left)):
            if _is_sequence(seq, seqs):
                return _check_repeat(seq, _fold(count, consts), line)
    if isinstance(node, ast.BinOp) and isinstance(_fold(node, consts), _Huge):
        if not any(isinstance(_fold(side, consts), _Huge) for side in (left, right)):
            return [Issue(line, f"constant '{ast.unparse(node)}' is too large (over 2**{MAX_INT_BITS}); "
                                "use only numbers the task needs")]
    return []


def _check_repeat(seq: ast.AST, count, line: int) -> List[Issue]:
    if not _is_literal_sequence(seq):
        return [Issue(line, "repeat only a literal list or string, e.g. [0] * 6 or '-' * 20; "
                            "build other lists with a comprehension, e.g. [x for x in objs]")]
    if not isinstance(count, int):
        return [Issue(line, f"repeat a sequence only a constant number of times (<= {MAX_REPEAT}), e.g. [0] * 6; "
                            "for a computed length use a comprehension, e.g. [0 for _ in objs]")]
    if count > MAX_REPEAT:
        return [Issue(line, f"sequence repeated {count} times (limit {MAX_REPEAT}); build only what you need")]
    return []


def _is_literal_sequence(node: ast.AST) -> bool:
    """A display whose length is fixed by the source: 'ab', b'ab', [0, 1], (0,)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes))
    return isinstance(node, (ast.List, ast.Tuple))


def _is_sequence(node: ast.AST, seqs: Set[str]) -> bool:
    """True if `node` is (or may be) a str, bytes, list or tuple, judged from the source alone."""
    if _is_literal_sequence(node) or isinstance(node, (ast.ListComp, ast.JoinedStr)):
        return True
    if isinstance(node, ast.Name):
        return node.id in seqs
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id in _SEQ_CALLS
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
        return _is_sequence(node.left, seqs) or _is_sequence(node.right, seqs)
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
        return _is_sequence(node.value, seqs)
    if isinstance(node, ast.IfExp):
        return _is_sequence(node.body, seqs) or _is_sequence(node.orelse, seqs)
    if isinstance(node, ast.NamedExpr):
        return _is_sequence(node.value, seqs)
    return False


def _sequence_names(tree: ast.Module) -> Set[str]:
    """Names bound anywhere to a sequence expression (flow-insensitive, to a fixed point)."""
    seqs: Set[str] = set()
    pairs = list(_bindings(tree))
    changed = True
    while changed:
        changed = False
        for target, value in pairs:
            if _is_sequence(value, seqs):
                new = _target_names(target) - seqs
                seqs |= new
                changed = changed or bool(new)
    return seqs


def _bindings(tree: ast.AST):
    """(target, value) for every plain assignment: =, annotated =, and :=."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            yield node.target, node.value


def _target_names(target: ast.AST) -> Set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}


def _loaded_names(node: ast.AST) -> Set[str]:
    return {n.id for n in _walk_scope([node]) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _walk_scope(nodes):
    """ast.walk over `nodes`, not descending into nested function definitions."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


_FOLD_OPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}


def _fold(node: ast.AST, consts: dict):
    """Value of a numeric constant expression (literals and top-level constants), else None.

    Returns _HUGE (compares as +inf) instead of computing an integer over MAX_INT_BITS bits.
    """
    memo = getattr(consts, "memo", None)
    if memo is None:
        return _fold_node(node, consts)
    if id(node) not in memo:
        memo[id(node)] = (node, _fold_node(node, consts))  # keep node alive so its id stays unique
    return memo[id(node)][1]


def _fold_node(node: ast.AST, consts: dict):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        value = consts.get(node.id)
        return value if isinstance(value, (int, float)) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _fold(node.operand, consts)
        return None if value is None else (-value if isinstance(node.op, ast.USub) else value)
    if isinstance(node, ast.BinOp) and type(node.op) in _FOLD_OPS:
        a, b = _fold(node.left, consts), _fold(node.right, consts)
        if a is None or b is None or (isinstance(node.op, ast.Pow) and abs(b) > 64):
            return None  # never compute a huge power here; _check_arith rejects it
        if isinstance(a, _Huge) or isinstance(b, _Huge) or _too_big(node.op, a, b):
            return _HUGE
        try:
            value = _FOLD_OPS[type(node.op)](a, b)
        except (ArithmeticError, ValueError):
            return None
        return _HUGE if isinstance(value, int) and value.bit_length() > MAX_INT_BITS else value
    return None


def _too_big(op: ast.operator, a, b) -> bool:
    """Would the integer result have more than MAX_INT_BITS bits? Decided before computing it."""
    if not (isinstance(a, int) and isinstance(b, int)):
        return False
    if isinstance(op, ast.Mult):
        return a.bit_length() + b.bit_length() > MAX_INT_BITS + 1
    if isinstance(op, ast.Pow):
        return b > 0 and a.bit_length() * b > MAX_INT_BITS + b
    return False


def _top_level_constants(tree: ast.Module) -> _Consts:
    consts = _Consts()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            consts[stmt.targets[0].id] = _fold(stmt.value, consts)
    return consts


def _is_constant_expr(node: ast.AST, consts: dict) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_constant_expr(e, consts) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_constant_expr(k, consts) for k in node.keys) and \
            all(_is_constant_expr(v, consts) for v in node.values)
    if isinstance(node, ast.Name):
        return node.id in consts
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "math"
    if isinstance(node, ast.UnaryOp):
        return _is_constant_expr(node.operand, consts)
    if isinstance(node, ast.BinOp):
        return _is_constant_expr(node.left, consts) and _is_constant_expr(node.right, consts)
    return False


def _bound_names(tree: ast.Module) -> Set[str]:
    """Every name the skill itself binds (functions, parameters, assignments, loop targets)."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (getattr(ast, "MatchAs", ()), getattr(ast, "MatchStar", ()))) and node.name:
            names.add(node.name)
        elif isinstance(node, getattr(ast, "MatchMapping", ())) and node.rest:
            names.add(node.rest)
    return names


def _check_top_level(tree: ast.Module, consts: dict) -> List[Issue]:
    issues: List[Issue] = []
    run = None
    seen: dict = {}
    for i, stmt in enumerate(tree.body):
        if isinstance(stmt, ast.FunctionDef):
            if stmt.name == "run":
                run = stmt
            continue
        if isinstance(stmt, ast.Assign):
            if not _is_constant_expr(stmt.value, seen):
                issues.append(Issue(stmt.lineno, "top-level assignments must be constants (numbers, strings, "
                                                 "lists, dicts); compute other values inside run()"))
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    seen[target.id] = consts.get(target.id)
            continue
        if i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # module docstring
        issues.append(Issue(stmt.lineno, "top level may only hold a docstring, constants and functions"))
    if run is None:
        issues.append(Issue(0, "missing `def run(robot, scene, skills, ...)`"))
    else:
        names = [a.arg for a in run.args.args[:3]]
        if names != ["robot", "scene", "skills"]:
            issues.append(Issue(run.lineno, "run must start with parameters (robot, scene, skills)"))
    return issues


def _check_recursion(funcs: Dict[str, ast.FunctionDef]) -> List[Issue]:
    """Recursion is an unbounded loop in disguise; every loop must be a bounded `for`."""
    calls = {name: _direct_calls(fn) & set(funcs) for name, fn in funcs.items()}
    issues: List[Issue] = []
    reported: Set[str] = set()

    def visit(name: str, path: List[str]) -> None:
        for callee in sorted(calls[name]):
            if callee in path:
                cycle = path[path.index(callee):] + [callee]
                if cycle[0] not in reported:
                    reported.update(cycle)
                    issues.append(Issue(funcs[cycle[0]].lineno, f"recursion is not allowed ({' -> '.join(cycle)}); "
                                                                "use a for loop over range(n)"))
            elif callee not in reported:
                visit(callee, path + [callee])

    for name in sorted(funcs):
        if name not in reported:
            visit(name, [name])
    return issues


def _direct_calls(fn: ast.FunctionDef) -> Set[str]:
    """Names called in fn's own body, not inside functions nested in it."""
    found: Set[str] = set()
    stack: List[ast.AST] = [*fn.body]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            found.add(node.func.id)
        stack.extend(ast.iter_child_nodes(node))
    return found


# --- values that grow from their own previous value inside a loop (WS4.9) ---------------------

def _check_loop_growth(tree: ast.Module, consts: dict, seqs: Set[str],
                       funcs: Dict[str, ast.FunctionDef]) -> List[Issue]:
    """`n = n * n` or `s = s * 2` in a loop squares a number or doubles a list every round.

    Within each loop, a growth edge t -> n means t's new value multiplies n by something that is
    not a small constant. A growth edge on a cycle (t feeds back into n) is rejected.
    """
    fixed = _fixed_names(tree, consts)
    growing = _growing_functions(funcs, fixed)
    found: Dict[tuple, Issue] = {}
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, *_COMPREHENSIONS)):
            continue
        assigns = list(_loop_assignments(loop))
        carried = {t for t, _, _ in assigns}
        edges: Dict[str, Set[str]] = {}
        sites = []
        for target, value, line in assigns:
            edges.setdefault(target, set()).update(_loaded_names(value))
            for name in _growth_names(value, fixed, carried, seqs, growing):
                sites.append((target, name, line))
        for target, name, line in sites:
            if _reaches(edges, name, target):
                found[(line, target)] = Issue(line, f"'{target}' grows from its own previous value by "
                                                    "multiplication inside a loop; a few rounds make a number "
                                                    "or list too big for a timeout to stop. Compute it from the "
                                                    "loop index (e.g. z = z0 + i * step) or scale by a float "
                                                    "constant such as 0.5")
    return list(found.values())


def _loop_assignments(loop: ast.AST):
    """(target name, value, line) for every name the loop body rebinds on each round."""
    if isinstance(loop, ast.For):
        nodes = list(_walk_scope(loop.body))
    else:
        nodes = list(_walk_scope([loop]))
    for node in nodes:
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            value = ast.BinOp(left=ast.Name(id=node.target.id, ctx=ast.Load()), op=node.op, right=node.value)
            yield node.target.id, value, node.lineno
        elif isinstance(node, ast.For):
            for name in _target_names(node.target):
                yield name, node.iter, node.lineno
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _target_names(target):
                    yield name, node.value, node.lineno


def _growth_names(expr: ast.AST, fixed: dict, carried: Set[str], seqs: Set[str], growing: Set[str]) -> Set[str]:
    """Names in `expr` that get multiplied (or raised to a power) by something that can grow them."""
    out: Set[str] = set()
    for node in _walk_scope([expr]):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            for side, other in ((node.left, node.right), (node.right, node.left)):
                if _grows(side, other, fixed, carried, seqs):
                    out |= _loaded_names(side)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if not _small_factor(node.right, fixed):
                out |= _loaded_names(node.left)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            if isinstance(node.left, ast.Name) and isinstance(node.right, ast.Name) and node.left.id == node.right.id:
                out.add(node.left.id)  # s + s doubles s
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in growing:
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                out |= _loaded_names(arg)
    return out


def _grows(side: ast.AST, other: ast.AST, fixed: dict, carried: Set[str], seqs: Set[str]) -> bool:
    """Does `side * other` make `side` bigger every round? Floats and loop invariants do not (for numbers)."""
    if _small_factor(other, fixed):
        return False
    if _loaded_names(other) & carried or isinstance(_fold(other, fixed), int):
        return True  # a product of two carried values (squaring), or repetition by a constant >= 2
    return bool(_loaded_names(side) & seqs)


def _small_factor(node: ast.AST, fixed: dict) -> bool:
    """A float constant, or an integer constant in [-1, 1]: multiplying by it never grows a value."""
    value = _fold(node, fixed)
    if isinstance(value, _Huge) or value is None:
        return False
    return isinstance(value, float) or abs(value) <= 1


def _fixed_names(tree: ast.Module, consts: dict) -> _Consts:
    """Top-level constants plus names bound exactly once, anywhere, to a constant (`gain = 0.5`)."""
    fixed = _Consts()
    fixed.update(consts)
    counts: Dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            counts[node.id] = counts.get(node.id, 0) + 1
        elif isinstance(node, ast.arg):
            counts[node.arg] = counts.get(node.arg, 0) + 2
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if counts.get(name) == 1 and name not in fixed:
                fixed[name] = _fold(node.value, consts)
    return fixed


def _growing_functions(funcs: Dict[str, ast.FunctionDef], fixed: dict) -> Set[str]:
    """Local functions that return a product of their own parameters, e.g. `return n * n`."""
    out: Set[str] = set()
    for name, fn in funcs.items():
        params = {a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]}
        for node in _walk_scope(fn.body):
            if isinstance(node, ast.Return) and node.value is not None:
                if _growth_names(node.value, fixed, params, set(), set()) & params:
                    out.add(name)
    return out


def _reaches(edges: Dict[str, Set[str]], start: str, goal: str) -> bool:
    seen: Set[str] = set()
    stack = [start]
    while stack:
        name = stack.pop()
        if name == goal:
            return True
        if name not in seen:
            seen.add(name)
            stack.extend(edges.get(name, ()))
    return False


# --- names bound to robot/scene data are read-only too (WS4.9) ---------------------------------

def _check_aliases(tree: ast.Module, funcs: Dict[str, ast.FunctionDef]) -> List[Issue]:
    """`objs = scene.objects; objs.clear()` changes the scene the runtime compares against."""
    aliases = _data_aliases(tree, funcs)
    issues: List[Issue] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATORS:
            root = _root_name(node.func.value)
            if root in aliases:
                issues.append(_alias_issue(root, aliases[root], f"'.{node.func.attr}()'", line))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            root = _root_name(node)
            if root in aliases:
                issues.append(_alias_issue(root, aliases[root], "item assignment", line))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id in aliases:
            in_place = isinstance(node.op, ast.BitOr) or isinstance(
                node.value, (ast.List, ast.Tuple, ast.ListComp, ast.Set, ast.Dict)) or _is_sequence(node.value, set())
            if in_place:
                issues.append(_alias_issue(node.target.id, aliases[node.target.id], "'+=' on a list", line))
    return issues


def _alias_issue(name: str, source: tuple, what: str, line: int) -> Issue:
    handle, bound_at = source
    return Issue(line, f"'{name}' is {handle} data (bound on line {bound_at}) and read-only ({what} would change "
                       f"it); copy it into a new name first, e.g. mine = list({name})")


def _data_aliases(tree: ast.Module, funcs: Dict[str, ast.FunctionDef]) -> Dict[str, tuple]:
    """Names (and local functions) that may hold robot/scene data: name -> (handle, line bound)."""
    pairs = [(t, v, getattr(t, "lineno", 0), False) for t, v in _bindings(tree)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.comprehension)):
            pairs.append((node.target, node.iter, getattr(node.target, "lineno", 0), True))
        elif isinstance(node, ast.Call):  # a local function's parameter is bound to the argument
            slots = [*enumerate(node.args), *((kw.arg, kw.value) for kw in node.keywords if kw.arg)]
            for slot, arg in slots:
                param = _param_name(node, slot, funcs)
                if param:
                    pairs.append((ast.Name(id=param, ctx=ast.Store()), arg, getattr(arg, "lineno", 0), False))
    returns = [(name, r.value) for name, fn in funcs.items()
               for r in _walk_scope(fn.body) if isinstance(r, ast.Return) and r.value is not None]
    aliases: Dict[str, tuple] = {}
    changed = True
    while changed:
        changed = False
        for target, value, line, loop in pairs:
            source = _data_source(value, aliases, loop)
            for name in (_target_names(target) - set(HANDLES) - set(aliases)) if source else ():
                aliases[name] = (source, line)
                changed = True
        for name, value in returns:
            source = _data_source(value, aliases, loop=False)
            if source and name not in aliases:
                aliases[name] = (source, funcs[name].lineno)
                changed = True
    return aliases


def _data_source(value: ast.AST, aliases: Dict[str, tuple], loop: bool) -> Optional[str]:
    """'scene'/'robot' if `value` is (part of) robot/scene data rather than a fresh copy, else None.

    A loop variable is an element, so `for o in sorted(scene.objects)` still yields scene objects.
    """
    if loop:
        for name in _loaded_names(value):
            if name in ("robot", "scene"):
                return name
            if name in aliases:
                return aliases[name][0]
        return None
    if isinstance(value, ast.NamedExpr):
        return _data_source(value.value, aliases, loop)
    if isinstance(value, ast.IfExp):
        return _data_source(value.body, aliases, loop) or _data_source(value.orelse, aliases, loop)
    if isinstance(value, ast.BoolOp):
        return next((s for s in (_data_source(v, aliases, loop) for v in value.values) if s), None)
    if isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Slice):
        return None  # a slice is a copy
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "copy":
        return None
    root = _root_name(value)
    if root in ("robot", "scene"):
        return root
    return aliases[root][0] if root in aliases else None
