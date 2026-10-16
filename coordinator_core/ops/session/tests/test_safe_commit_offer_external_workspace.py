"""compute_offer declines, naming the key, when cwd is inside a registered external workspace."""

from __future__ import annotations

import pytest

from coordinator_core.ops.session import safe_commit_offer as sco


@pytest.fixture()
def no_claims(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("claim index read for an external workspace")

    monkeypatch.setattr(sco.claim_index, "commit_set", boom)
    return calls


def test_inside_workspace_declines_naming_key(tmp_path, monkeypatch, no_claims):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "dirty.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "coordinator_core.machine_resolver.external_workspace_containing",
        lambda p: "p4_fifa_slice" if str(p).startswith(str(ws)) else None,
    )
    offer = sco.compute_offer("s-1", str(ws / "sub"))
    assert offer["safe_paths"] == []
    assert "p4_fifa_slice" in offer["declined"]
    assert no_claims == []


def test_handler_refuses_inside_workspace(tmp_path, monkeypatch, no_claims):
    monkeypatch.setattr(
        "coordinator_core.machine_resolver.external_workspace_containing",
        lambda p: "p4_fifa_slice",
    )
    out = sco._handler({"cwd": str(tmp_path), "session_id": "s-1", "message": "m"})
    assert "p4_fifa_slice" in out["error"]


def test_outside_workspace_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.machine_resolver.external_workspace_containing",
        lambda p: None,
    )
    offer = sco.compute_offer("s-1", str(tmp_path))
    assert "declined" not in offer
