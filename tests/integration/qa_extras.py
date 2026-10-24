"""Optional extras in the QA suites: skip in `make check`, fail in `make check-heavy`.

`make check` runs without mujoco/placo/opencv/fastapi, so a test that needs one skips. The heavy CI
job (`SKILLSMITH_HEAVY=1`, every extra installed) exists to run exactly those tests; if an import
still fails there (a renamed module, a missing system lib, a broken wheel), a skip would turn the
job green while testing nothing. So under SKILLSMITH_HEAVY a missing extra is a failure.
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType
from typing import NoReturn, Optional

import pytest


def heavy() -> bool:
    return os.environ.get("SKILLSMITH_HEAVY") == "1"


def missing_extra(extra: str, err: ImportError) -> NoReturn:
    """Skip (fail under SKILLSMITH_HEAVY) because `extra` did not import."""
    reason = f"{extra} extra not installed ({err.name or err}): uv pip install -e '.[{extra}]'"
    if heavy():
        pytest.fail(f"SKILLSMITH_HEAVY=1 but {reason}", pytrace=False)
    pytest.skip(reason, allow_module_level=True)


def built(err: Optional[Exception], extra: str, waiting: str) -> None:
    """Act on the error from building an optional backend (None = it built): an ImportError is a
    missing extra, a NotImplementedError means the owning workstream has not landed it yet."""
    if isinstance(err, ImportError):
        missing_extra(extra, err)
    if err is not None:
        pytest.skip(waiting)


def require(module: str, extra: str) -> ModuleType:
    """`pytest.importorskip(module)` that fails under SKILLSMITH_HEAVY."""
    try:
        return importlib.import_module(module)
    except ImportError as e:
        missing_extra(extra, e)
