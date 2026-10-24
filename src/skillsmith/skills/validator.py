"""Static checks on Droid-written skill code. See docs/INTERFACES.md §4.

This is the first gate, not a security boundary on its own: the runtime also strips
builtins, the SafetyGuard checks every target, and robotd clamps joints.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Dict, List

ROBOT_API = {"move_to", "grasp", "release", "look", "home", "position", "holding", "note"}
SCENE_API = {"find", "require", "objects"}
SKILLS_API = {"call"}
HANDLES: Dict[str, set] = {"robot": ROBOT_API, "scene": SCENE_API, "skills": SKILLS_API}

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
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        for kind, msg in _BANNED_NODES.items():
            if isinstance(node, kind):
                issues.append(Issue(line, msg))
        if isinstance(node, ast.Name):
            issues += _check_name(node, parents.get(node), line)
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            issues.append(Issue(line, f"attribute '{node.attr}' is private"))
        elif isinstance(node, ast.Call):
            issues += _check_range(node, max_range, line)
    issues += _check_top_level(tree)
    return sorted(issues, key=lambda i: i.line)


def _check_name(node: ast.Name, parent, line: int) -> List[Issue]:
    if node.id in BANNED_NAMES or node.id.startswith("__"):
        return [Issue(line, f"'{node.id}' is not allowed")]
    if node.id not in HANDLES:
        return []
    if isinstance(node.ctx, (ast.Store, ast.Del)):
        return [Issue(line, f"do not reassign '{node.id}'")]
    if isinstance(parent, ast.Attribute) and parent.value is node:
        if parent.attr not in HANDLES[node.id]:
            allowed = ", ".join(sorted(HANDLES[node.id]))
            return [Issue(line, f"{node.id}.{parent.attr} is not in the API (allowed: {allowed})")]
        return []
    if isinstance(parent, ast.Call) and node in parent.args:
        return []  # passing robot/scene/skills into a helper function
    if isinstance(parent, ast.keyword):
        return []
    return [Issue(line, f"use '{node.id}' only as {node.id}.<method> or as a call argument")]


def _check_range(node: ast.Call, max_range: int, line: int) -> List[Issue]:
    if not (isinstance(node.func, ast.Name) and node.func.id == "range"):
        return []
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)) and arg.value > max_range:
            return [Issue(line, f"range bound {arg.value} > {max_range}")]
    return []


def _check_top_level(tree: ast.Module) -> List[Issue]:
    issues: List[Issue] = []
    run = None
    for i, stmt in enumerate(tree.body):
        if isinstance(stmt, ast.FunctionDef):
            if stmt.name == "run":
                run = stmt
            continue
        if isinstance(stmt, ast.Assign):
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
