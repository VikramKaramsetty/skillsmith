"""QA.6: a missing extra skips in `make check` but fails the heavy job (SKILLSMITH_HEAVY=1)."""

from __future__ import annotations

import pytest
from qa_extras import built, require

MISSING = "skillsmith_qa_no_such_module"


def test_missing_extra_skips_by_default(monkeypatch):
    monkeypatch.delenv("SKILLSMITH_HEAVY", raising=False)
    with pytest.raises(pytest.skip.Exception, match=r"sim extra not installed .*\.\[sim\]"):
        require(MISSING, "sim")


def test_missing_extra_fails_the_heavy_job(monkeypatch):
    monkeypatch.setenv("SKILLSMITH_HEAVY", "1")
    with pytest.raises(pytest.fail.Exception, match=r"SKILLSMITH_HEAVY=1 but sim extra not installed"):
        require(MISSING, "sim")
    with pytest.raises(pytest.fail.Exception, match="kin extra"):
        built(ImportError("no placo", name="placo"), "kin", "waiting on WS2.2")


def test_unbuilt_backend_still_skips_in_the_heavy_job(monkeypatch):
    """Heavy installs every extra but cannot land another workstream's code: that stays a skip."""
    monkeypatch.setenv("SKILLSMITH_HEAVY", "1")
    with pytest.raises(pytest.skip.Exception, match="waiting on WS2.4"):
        built(NotImplementedError("make_sim_env"), "sim", "waiting on WS2.4")
    built(None, "sim", "waiting on WS2.4")  # built: no skip, no fail


def test_require_returns_the_module():
    assert require("json", "dev").__name__ == "json"
