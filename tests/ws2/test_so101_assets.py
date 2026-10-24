"""WS2.1: the vendoring script (offline) and the vendored SO101 URDF/MJCF (skipped until fetched)."""

from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets" / "so101"
ROBOT = yaml.safe_load((ROOT / "config" / "robot.yaml").read_text())["robot"]


def _load_fetch():
    spec = importlib.util.spec_from_file_location("fetch_so101", ASSETS / "fetch_so101.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fetch = _load_fetch()

URDF = """<robot name="so101">
  <link name="base"><visual><geometry><mesh filename="assets/base.stl"/></geometry></visual>
    <collision><geometry><mesh filename="assets/base.stl"/></geometry></collision></link>
  <link name="arm"><visual><geometry>
    <mesh filename="package://so101/assets/arm.stl" scale="1 1 1"/></geometry></visual></link>
</robot>"""


# --- fetch script, no network ----------------------------------------------------------------------

def test_urdf_mesh_paths_unique_in_order():
    assert fetch.urdf_mesh_paths(URDF) == ["assets/base.stl", "package://so101/assets/arm.stl"]


def test_rewrite_urdf_meshes_points_at_urdf_mesh_dir():
    out = fetch.rewrite_urdf_meshes(URDF)
    assert fetch.urdf_mesh_paths(out) == ["urdf_meshes/base.stl", "urdf_meshes/arm.stl"]
    assert 'scale="1 1 1"' in out


def _fake_repos(tmp: Path):
    arm = tmp / "so-arm100"
    (arm / fetch.SO_ARM100_DIR / "assets").mkdir(parents=True)
    (arm / fetch.SO_ARM100_DIR / fetch.URDF_NAME).write_text(URDF)
    (arm / fetch.SO_ARM100_DIR / "assets" / "base.stl").write_bytes(b"base")
    (arm / fetch.SO_ARM100_DIR / "assets" / "arm.stl").write_bytes(b"arm")
    (arm / fetch.SO_ARM100_DIR / "assets" / "unused.stl").write_bytes(b"unused")
    (arm / "LICENSE").write_text("Apache arm")
    men = tmp / "menagerie"
    (men / fetch.MENAGERIE_DIR / "assets").mkdir(parents=True)
    (men / fetch.MENAGERIE_DIR / "scene.xml").write_text('<mujoco><include file="so101.xml"/></mujoco>')
    (men / fetch.MENAGERIE_DIR / "so101.xml").write_text("<mujoco/>")
    (men / fetch.MENAGERIE_DIR / "README.md").write_text("menagerie readme")
    (men / fetch.MENAGERIE_DIR / "LICENSE").write_text("Apache menagerie")
    (men / fetch.MENAGERIE_DIR / "assets" / "base.stl").write_bytes(b"mj base")
    return arm, men


def test_vendor_layout_licenses_and_sources(tmp_path):
    arm, men = _fake_repos(tmp_path)
    out = tmp_path / "out"
    files = fetch.vendor_urdf(arm, out) + fetch.vendor_mjcf(men, out)
    sources = json.loads(fetch.write_sources(out, {"so_arm100": "a" * 40, "menagerie": "b" * 40}, files).read_text())

    assert fetch.urdf_mesh_paths((out / fetch.URDF_NAME).read_text()) == ["urdf_meshes/base.stl", "urdf_meshes/arm.stl"]
    assert (out / "urdf_meshes" / "base.stl").read_bytes() == b"base"      # URDF mesh kept apart from MJCF mesh
    assert (out / "assets" / "base.stl").read_bytes() == b"mj base"
    assert not (out / "urdf_meshes" / "unused.stl").exists()               # only referenced meshes
    assert (out / "LICENSE.so-arm100").read_text() == "Apache arm"         # repo-root fallback
    assert (out / "LICENSE.menagerie").read_text() == "Apache menagerie"   # model-folder LICENSE wins
    assert (out / "README.menagerie.md").read_text() == "menagerie readme"
    assert not (out / "README.md").exists()
    assert sources["so_arm100"]["commit"] == "a" * 40 and sources["menagerie"]["commit"] == "b" * 40
    assert set(sources["sha256"]) == {str(f.relative_to(out)) for f in files}


def test_find_license_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        fetch.find_license(tmp_path)


# --- vendored files (run once `python assets/so101/fetch_so101.py` has been run) -------------------

vendored = pytest.mark.skipif(not (ROOT / ROBOT["urdf"]).is_file(),
                              reason="SO101 assets not vendored yet: run python assets/so101/fetch_so101.py")


@vendored
def test_vendored_licenses_and_checksums():
    assert (ASSETS / "LICENSE.so-arm100").stat().st_size > 0
    assert (ASSETS / "LICENSE.menagerie").stat().st_size > 0
    sources = json.loads((ASSETS / "SOURCES.json").read_text())
    for key in ("so_arm100", "menagerie"):
        assert len(sources[key]["commit"]) == 40
    for rel, digest in sources["sha256"].items():
        assert fetch.sha256(ASSETS / rel) == digest, f"{rel} differs from upstream; re-run fetch_so101.py"


@vendored
def test_vendored_urdf_has_config_joints_frame_and_meshes():
    urdf_path = ROOT / ROBOT["urdf"]
    root = ET.parse(urdf_path).getroot()
    joints = {j.get("name") for j in root.iter("joint") if j.get("type") in ("revolute", "continuous", "prismatic")}
    assert set(ROBOT["joint_names"]) <= joints
    assert ROBOT["ee_frame"] in {link.get("name") for link in root.iter("link")}
    for mesh in fetch.urdf_mesh_paths(urdf_path.read_text()):
        assert (urdf_path.parent / mesh).is_file(), mesh


@vendored
def test_vendored_mjcf_loads_in_mujoco():
    mujoco = pytest.importorskip("mujoco")
    model = mujoco.MjModel.from_xml_path(str(ROOT / ROBOT["mjcf"]))
    names = {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, i) for i in range(model.njnt)}
    assert set(ROBOT["joint_names"]) <= names
