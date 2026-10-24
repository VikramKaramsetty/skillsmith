"""WS2.2: a missing URDF says how to vendor it (and reads as 'WS2 not ready' to the conformance suite)."""

import pytest


def test_missing_urdf_raises_not_implemented(tmp_path):
    pytest.importorskip("placo")
    from skillsmith.kinematics import PlacoKinematics

    with pytest.raises(NotImplementedError, match="fetch_so101.py"):
        PlacoKinematics(str(tmp_path / "nope.urdf"))
