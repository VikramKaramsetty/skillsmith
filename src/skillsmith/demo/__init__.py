"""`make demo-fake`: the whole fail → repair → verify story with no hardware.

The "real" world is a FakeWorld with rim_slip=True; the sim check world is a FakeWorld
built from the observed scene WITHOUT rim slip. So the first draft (rim grip) passes the
sim gate, fails on the "real" arm (cup slips out), and the camera check catches it. The
scripted author then supplies the fix (mid-height grip via pick_place). Swap
ScriptedAuthor for DroidAuthor (`--author droid`) to have Droid write the fix for real.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from skillsmith.config import REPO_ROOT

DEMO_DIR = Path(__file__).parent


def demo_sources() -> tuple[str, str]:
    return (DEMO_DIR / "stack_v1.py").read_text(), (DEMO_DIR / "stack_v2.py").read_text()


def temp_library() -> Path:
    """Copy the seed library to a temp dir so demos never touch skills/library."""
    root = Path(tempfile.mkdtemp(prefix="skillsmith-lib-"))
    shutil.copytree(REPO_ROOT / "skills" / "library", root / "library")
    return root / "library"
