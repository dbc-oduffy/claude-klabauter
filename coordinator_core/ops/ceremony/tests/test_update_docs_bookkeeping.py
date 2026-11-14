"""Tests for "ceremony.update_docs_bookkeeping": registry, composition of the two ops, and
the report-by-default contract."""

from __future__ import annotations

import pytest

import coordinator_core.ops.ceremony.update_docs_bookkeeping as ub
from coordinator_core.ipc import _REGISTRY

_OP = "ceremony.update_docs_bookkeeping"


def test_op_registered():
    assert _OP in _REGISTRY


@pytest.fixture
def seen(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    calls: dict = {}

    def worklist(params, repo_root=None):
        calls["worklist"] = (params, repo_root)
        return {"agent_worklist": [{"kind": "doc_drift_review"}]}

    def emit(params, repo_root=None):
        calls["emit"] = (params, repo_root)
        return {"results": [], "refused": []}

    monkeypatch.setattr(ub, "_ceremony_update_docs_worklist", worklist)
    monkeypatch.setattr(ub, "_docindex_emit", emit)
    return root, calls


def test_default_call_reports_and_writes_nothing(seen):
    root, calls = seen
    out = ub._ceremony_update_docs_bookkeeping({}, repo_root=root / ".git")
    assert out == {
        "inventory": {"agent_worklist": [{"kind": "doc_drift_review"}]},
        "index_regen": {"results": [], "refused": []},
    }
    assert calls["worklist"][0] == {"ack": False}
    emit_params = calls["emit"][0]
    assert emit_params["write"] is False and emit_params["rebaseline"] is False
    assert emit_params["target_root"] == str(root)


def test_flags_route_to_the_owning_op(seen):
    root, calls = seen
    ub._ceremony_update_docs_bookkeeping(
        {"ack": True, "write": True, "rebaseline": True}, repo_root=root / ".git"
    )
    assert calls["worklist"][0] == {"ack": True}
    assert calls["emit"][0]["write"] is True and calls["emit"][0]["rebaseline"] is True


def test_missing_repo_root_is_refused():
    with pytest.raises(ValueError, match="repo_root is None"):
        ub._ceremony_update_docs_bookkeeping({}, repo_root=None)
