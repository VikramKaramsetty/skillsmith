"""Droid's permission policy, its tools, lessons, and the SDK wiring (no droid binary needed)."""

from pathlib import Path

import pytest

from skillsmith.config import load_config
from skillsmith.demo import demo_sources
from skillsmith.events import NullBus
from skillsmith.hal.base import make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair.droid_tools import RepairTools, append_lesson, decide
from skillsmith.tasks import parse_task

cfg = load_config()
TASK = parse_task("stack the blue cup on the red cup")


# Stand-ins with the same class names and fields as droid_sdk's permission actions.
class EditAction:
    def __init__(self, file_path):
        self.file_path = file_path


class CreateFile(EditAction):
    pass


class ApplyPatchAction(EditAction):
    def __init__(self, file_path, files=()):
        super().__init__(file_path)
        self.files = list(files)


class McpToolAction:
    def __init__(self, tool_name, server_name):
        self.tool_name, self.server_name, self.actual_tool_name = tool_name, server_name, tool_name


class ExecuteAction:
    full_command = "rm -rf /"


def test_policy_allows_only_skill_py_and_skillsmith_tools(tmp_path):
    ws = tmp_path
    assert decide(EditAction(str(ws / "skill.py")), ws)[0]
    assert decide(EditAction("skill.py"), ws)[0]
    assert not decide(EditAction(str(ws / "AGENTS.md")), ws)[0]
    assert not decide(CreateFile(str(ws / "library" / "evil.py")), ws)[0]
    assert not decide(EditAction(str(ws / ".." / "skill.py")), ws)[0]
    assert not decide(ApplyPatchAction("skill.py", files=[EditAction("/etc/hosts")]), ws)[0]
    assert decide(McpToolAction("simulate_skill", "skillsmith"), ws)[0]
    assert not decide(McpToolAction("send_email", "gmail"), ws)[0]
    ok, why = decide(ExecuteAction(), ws)
    assert not ok and "simulate_skill" in why


def tools_for(tmp_path, source):
    (tmp_path / "skill.py").write_text(source)
    scene = FakeWorld.demo(cfg).observe()
    t = RepairTools(cfg, NullBus(), tmp_path, TASK, {}, lambda s: make_env("fake", cfg, s), sim_budget=2)
    t.update(scene, 1, {"pick_place": (Path(cfg.path("skills/library/pick_place.py"))).read_text()})
    return t


def test_tools_validate_and_simulate(tmp_path):
    _, v2 = demo_sources()
    t = tools_for(tmp_path, v2)
    assert t.validate_skill().startswith("PASS")
    assert t.simulate_skill().startswith("PASS")
    assert "pick_place" in t.list_library() and "def run" in t.read_library_skill("pick_place")
    assert "blue cup" in t.get_scene()


def test_simulate_refuses_bad_code_and_respects_budget(tmp_path):
    t = tools_for(tmp_path, "import os\n" + demo_sources()[1])
    assert t.validate_skill().startswith("FAIL")
    assert t.simulate_skill().startswith("Not simulated")
    (tmp_path / "skill.py").write_text(demo_sources()[1])
    t.simulate_skill()
    t.simulate_skill()
    assert "budget" in t.simulate_skill()


def test_lessons_append_once(tmp_path):
    path = tmp_path / "lessons.md"
    assert append_lesson(path, "stack", "Grip cups at mid-height; rim grips slip.")
    assert not append_lesson(path, "stack", "Grip cups at mid-height; rim grips slip.")
    assert not append_lesson(path, "stack", "   ")
    assert path.read_text().count("mid-height") == 1


def test_lessons_are_stripped_of_links_html_and_code(tmp_path):
    path = tmp_path / "lessons.md"
    lesson = ("Grip [low](http://x.io) ![f](/etc/passwd) <img src=/etc/hosts> see https://evil.example\n"
              "```python\nopen('/etc/passwd')\n``` [![a](/b)](/c) at mid-height.")
    assert append_lesson(path, "stack <b>cups</b>", lesson)
    line = path.read_text().splitlines()[-1]
    assert line.endswith("(stack cups): Grip low f see a at mid-height.")
    assert not any(bad in line for bad in ("](", "<", "http", "/etc", "```", "open("))
    assert append_lesson(path, "stack", "x" * 1000)
    assert len(path.read_text().splitlines()[-1].split(": ", 1)[1]) == 300
    assert not append_lesson(path, "stack", "```\n![x](/etc/passwd)\n```")  # nothing left after cleaning
    assert "/etc" not in path.read_text()


# ---- against the real droid_sdk classes (installed in the heavy CI job) ----------------------
class FakeRequest:
    def __init__(self, actions, options):
        self.actions, self.options, self.responses = actions, options, []

    def respond(self, outcome, *, comment=None):
        self.responses.append((outcome, comment))
        return outcome


def test_sdk_config_and_permission_handler(tmp_path):
    sdk = pytest.importorskip("droid_sdk")
    from skillsmith.repair.droid_sdk import DroidSDKAuthor

    bus = NullBus()
    author = DroidSDKAuthor(cfg, bus, lambda s: make_env("fake", cfg, s))
    t = tools_for(tmp_path, demo_sources()[1])
    config = author.build_config(t)
    assert config.autonomy == sdk.Autonomy.HIGH and len(config.mcp_servers) == 1
    allow = list(config.restrict_tools)
    assert {"ToolSearch", "Script", "WaitForScript", "TodoWrite"} <= set(allow)  # SDK may reorder
    assert "skillsmith___write_skill" in allow and "skillsmith___simulate_skill" in allow
    assert not {"Read", "Execute", "ApplyPatch", "FetchUrl", "WebSearch", "Task"} & set(allow)

    handler = author.build_interactions(tmp_path, t).on_permission
    Opt = type("Opt", (), {})
    options = []
    for value in (sdk.ToolConfirmationOutcome.PROCEED_ONCE, sdk.ToolConfirmationOutcome.CANCEL):
        o = Opt()
        o.value = value
        options.append(o)
    ok = FakeRequest([EditAction("skill.py")], options)
    handler(ok)
    bad = FakeRequest([ExecuteAction()], options)
    handler(bad)
    assert ok.responses[0][0] == sdk.ToolConfirmationOutcome.PROCEED_ONCE
    assert bad.responses[0][0] == sdk.ToolConfirmationOutcome.CANCEL and "Refused" in bad.responses[0][1]
    assert [e["data"]["allowed"] for e in bus.events if e["type"] == "droid.permission"] == [True, False]
    author.close()


def test_read_file_stays_inside_workspace(tmp_path):
    t = tools_for(tmp_path, demo_sources()[1])
    (tmp_path / "AGENTS.md").write_text("rules")
    assert t.read_file("AGENTS.md") == "rules"
    assert "def run" in t.read_file("skill.py")
    assert t.read_file("../secret.txt").startswith("Refused")
    assert t.read_file("/etc/passwd").startswith("Refused")
    assert t.read_file("missing.md").startswith("No file")


def test_write_skill_is_the_write_path_and_validates(tmp_path):
    t = tools_for(tmp_path, "placeholder")
    _, v2 = demo_sources()
    assert "PASS" in t.write_skill(v2)
    assert (tmp_path / "skill.py").read_text() == v2
    bad = t.write_skill("import os\n" + v2)
    assert "FAIL" in bad and "imports are not allowed" in bad


def test_new_tools_are_exposed_to_droid(tmp_path):
    names = [name for name, _, _ in tools_for(tmp_path, demo_sources()[1]).functions()]
    assert names[:2] == ["read_file", "write_skill"]
