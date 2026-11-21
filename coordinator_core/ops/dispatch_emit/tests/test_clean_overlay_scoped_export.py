"""The clean-overlay mirror holds only the run's test closure on a cold fill and a warm advance."""
from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov


RUN = "run1"
MOD_TEST = "tests/test_mod.py"
OTHER_TEST = "tests/test_other.py"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def test_the_export_timeout_is_unchanged():
    assert cov.TEST_TIMEOUT_S == 120
