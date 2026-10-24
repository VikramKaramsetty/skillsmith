"""Vendor the SO101 robot description into assets/so101/. OWNER: WS2 (WS2.1).

Two upstream sources, both copied unmodified except where noted:

- TheRobotStudio/SO-ARM100 `Simulation/SO101/so101_new_calib.urdf` plus the meshes it references.
  Used by placo (`kinematics.py`). Meshes go to `urdf_meshes/` and the URDF's `assets/` mesh paths
  are rewritten to match, so they never collide with Menagerie's `assets/` meshdir.
- google-deepmind/mujoco_menagerie `robotstudio_so101/` (`scene.xml`, `so101.xml`, `assets/`).
  Used by the MuJoCo sim (`hal/sim.py`). Copied verbatim.

Each upstream LICENSE is copied verbatim and `SOURCES.json` records commit SHAs and sha256 per file.

    python assets/so101/fetch_so101.py                 # upstream default branches
    python assets/so101/fetch_so101.py --so-arm100-ref <sha> --menagerie-ref <sha>

Needs only `git` and network access. Re-running overwrites the vendored files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent

SO_ARM100_URL = "https://github.com/TheRobotStudio/SO-ARM100.git"
SO_ARM100_DIR = "Simulation/SO101"
URDF_NAME = "so101_new_calib.urdf"
URDF_MESH_DIR = "urdf_meshes"

MENAGERIE_URL = "https://github.com/google-deepmind/mujoco_menagerie.git"
MENAGERIE_DIR = "robotstudio_so101"

LICENSE_NAMES = ("LICENSE", "LICENSE.txt", "LICENSE.md")


def sparse_clone(url: str, subdir: str, ref: str, dest: Path) -> str:
    """Shallow sparse clone of `subdir` at `ref` into `dest`; returns the resolved commit SHA."""
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    git = ["git", "-C", str(dest)]
    subprocess.run([*git, "remote", "add", "origin", url], check=True)
    subprocess.run([*git, "sparse-checkout", "set", "--no-cone", f"/{subdir}/", "/LICENSE*"], check=True)
    subprocess.run([*git, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref], check=True)
    subprocess.run([*git, "checkout", "-q", "FETCH_HEAD"], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def urdf_mesh_paths(urdf_text: str) -> List[str]:
    """Mesh filenames referenced by a URDF, in order, without duplicates."""
    seen: List[str] = []
    for mesh in ET.fromstring(urdf_text).iter("mesh"):
        name = mesh.get("filename", "")
        if name and name not in seen:
            seen.append(name)
    return seen


def rewrite_urdf_meshes(urdf_text: str, mesh_dir: str = URDF_MESH_DIR) -> str:
    """Point every relative mesh filename at `mesh_dir/<basename>` (package:// and absolute paths too)."""
    def repl(m: re.Match) -> str:
        return f'{m.group(1)}{mesh_dir}/{Path(m.group(2)).name}"'
    return re.sub(r'(<mesh\b[^>]*\bfilename=")([^"]+)"', repl, urdf_text)


def find_license(*dirs: Path) -> Path:
    """First LICENSE file in `dirs` (most specific first)."""
    for d in dirs:
        for name in LICENSE_NAMES:
            if (d / name).is_file():
                return d / name
    raise FileNotFoundError(f"no LICENSE in {[str(d) for d in dirs]}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vendor_urdf(repo: Path, out: Path) -> List[Path]:
    src_dir = repo / SO_ARM100_DIR
    text = (src_dir / URDF_NAME).read_text()
    written: List[Path] = []
    (out / URDF_MESH_DIR).mkdir(parents=True, exist_ok=True)
    for mesh in urdf_mesh_paths(text):
        rel = mesh.split("://", 1)[-1].lstrip("/") if "://" in mesh else mesh
        src = src_dir / rel
        if not src.is_file():
            src = src_dir / "assets" / Path(rel).name
        dst = out / URDF_MESH_DIR / Path(rel).name
        shutil.copyfile(src, dst)
        written.append(dst)
    (out / URDF_NAME).write_text(rewrite_urdf_meshes(text))
    written.append(out / URDF_NAME)
    lic = out / "LICENSE.so-arm100"
    shutil.copyfile(find_license(src_dir, repo), lic)
    written.append(lic)
    return written


def vendor_mjcf(repo: Path, out: Path) -> List[Path]:
    src_dir = repo / MENAGERIE_DIR
    written: List[Path] = []
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file() or src.name in LICENSE_NAMES:
            continue
        rel = src.relative_to(src_dir)
        if rel.name == "README.md":
            rel = Path("README.menagerie.md")  # our README.md describes the whole folder
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        written.append(dst)
    lic = out / "LICENSE.menagerie"
    shutil.copyfile(find_license(src_dir, repo), lic)
    written.append(lic)
    return written


def write_sources(out: Path, shas: Dict[str, str], files: List[Path]) -> Path:
    sources = {
        "so_arm100": {"url": SO_ARM100_URL, "commit": shas["so_arm100"], "path": f"{SO_ARM100_DIR}/{URDF_NAME}",
                      "modified": f"mesh filenames rewritten to {URDF_MESH_DIR}/<basename>"},
        "menagerie": {"url": MENAGERIE_URL, "commit": shas["menagerie"], "path": MENAGERIE_DIR, "modified": None},
        "sha256": {str(f.relative_to(out)): sha256(f) for f in sorted(files)},
    }
    path = out / "SOURCES.json"
    path.write_text(json.dumps(sources, indent=2) + "\n")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--so-arm100-ref", default="main")
    ap.add_argument("--menagerie-ref", default="main")
    ap.add_argument("--out", type=Path, default=HERE)
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        arm_repo, men_repo = Path(tmp) / "so-arm100", Path(tmp) / "menagerie"
        shas = {"so_arm100": sparse_clone(SO_ARM100_URL, SO_ARM100_DIR, args.so_arm100_ref, arm_repo),
                "menagerie": sparse_clone(MENAGERIE_URL, MENAGERIE_DIR, args.menagerie_ref, men_repo)}
        files = vendor_urdf(arm_repo, args.out) + vendor_mjcf(men_repo, args.out)
    print(f"vendored {len(files)} files into {args.out}; provenance in {write_sources(args.out, shas, files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
