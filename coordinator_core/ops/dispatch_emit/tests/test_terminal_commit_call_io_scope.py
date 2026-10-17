"""The dispatch.terminal_commit handler runs inside one call_io_scope and closes it on exit."""

import pytest

from coordinator_core.git import call_io_scope as cis
from coordinator_core.ops.dispatch_emit import terminal_commit as tc


def test_scope_open_during_handler_and_closed_after(monkeypatch):
    seen = []

    def probe(*args, **kwargs):
        seen.append(cis.scope_open())
        return {"committed": False}

    monkeypatch.setattr(tc, "_terminal_commit", probe)
    assert not cis.scope_open()
    tc._handler({"script_path": "x"})
    assert seen == [True]
    assert not cis.scope_open()


def test_scope_closed_after_handler_raises(monkeypatch):
    seen = []

    def boom(*args, **kwargs):
        seen.append(cis.scope_open())
        raise RuntimeError("boom")

    monkeypatch.setattr(tc, "_terminal_commit", boom)
    with pytest.raises(RuntimeError):
        tc._handler({"script_path": "x"})
    assert seen == [True]
    assert not cis.scope_open()
