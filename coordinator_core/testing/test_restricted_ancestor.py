import os

from coordinator_core.testing import scoped_pytest


def test_watches_parent_when_process_enumeration_is_denied(monkeypatch):
    def denied():
        raise PermissionError("process enumeration denied")

    monkeypatch.setattr(scoped_pytest.psutil, "pids", denied)
    monkeypatch.setattr(scoped_pytest.psutil, "_LOWEST_PID", None, raising=False)
    assert scoped_pytest.ancestor_chain()[0][0] == os.getppid()
