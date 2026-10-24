"""Static checks on Droid-written skill code. See docs/INTERFACES.md §4.

This is the first gate, not a security boundary on its own: the runtime also strips
builtins, the SafetyGuard checks every target, and robotd clamps joints. It is an allow-list:
only names the runtime provides, only the skill API on the handles, and every rejection says
what to write instead.
"""

from __future__ import annotations

import ast
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
             "update", "setdefault", "popitem", "add", "discard"}

MAX_EXPONENT = 10
MAX_REPEAT = 1000


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
            issues += _check_range(node, max_range, consts, line)
        elif isinstance(node, (ast.BinOp, ast.AugAssign)):
            issues += _check_arith(node, consts, line)
        elif isinstance(node, ast.FunctionDef) and node.decorator_list:
            issues.append(Issue(line, "decorators are not allowed; call helper functions directly"))
    issues += _check_top_level(tree, consts)
    issues += _check_recursion(funcs)
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
    root = _root_name(node.value)
    if attr in _MUTATORS and root in HANDLES and isinstance(parent, ast.Call) and parent.func is node:
        return [Issue(line, f"{root} data is read-only ('.{attr}()'); copy it first, e.g. "
                            "objs = list(scene.objects)")]
    return []


def _root_name(node: ast.AST) -> Optional[str]:
    """`scene` for `scene.objects[0].label`, `scene.require('x').z` and the like."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _check_range(node: ast.Call, max_range: int, consts: dict, line: int) -> List[Issue]:
    if not (isinstance(node.func, ast.Name) and node.func.id == "range"):
        return []
    for arg in node.args:
        value = _fold(arg, consts)
        if isinstance(value, (int, float)) and value > max_range:
            return [Issue(line, f"range bound {value} > {max_range}; loop over at most {max_range} items")]
    return []


def _check_arith(node, consts: dict, line: int) -> List[Issue]:
    """Operations a wall-clock timeout cannot interrupt: huge powers, shifts and repetitions."""
    right = node.right if isinstance(node, ast.BinOp) else node.value
    if isinstance(node.op, ast.LShift):
        return [Issue(line, "bit shifts are not allowed; use plain arithmetic")]
    if isinstance(node.op, ast.Pow):
        exp = _fold(right, consts)
        if not (isinstance(exp, (int, float)) and abs(exp) <= MAX_EXPONENT):
            return [Issue(line, f"exponent must be a number with |e| <= {MAX_EXPONENT}; "
                                "use math.pow(x, y) or math.sqrt(x) otherwise")]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        for seq, count in ((node.left, node.right), (node.right, node.left)):
            n = _fold(count, consts)
            if _is_sequence(seq) and isinstance(n, int) and n > MAX_REPEAT:
                return [Issue(line, f"sequence repeated {n} times (limit {MAX_REPEAT}); build only what you need")]
    return []


def _is_sequence(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes))
    return isinstance(node, (ast.List, ast.Tuple, ast.ListComp, ast.JoinedStr))


_FOLD_OPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}


def _fold(node: ast.AST, consts: dict):
    """Value of a numeric constant expression (literals and top-level constants), else None."""
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
        try:
            return _FOLD_OPS[type(node.op)](a, b)
        except (ArithmeticError, ValueError):
            return None
    return None


def _top_level_constants(tree: ast.Module) -> dict:
    consts: dict = {}
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
