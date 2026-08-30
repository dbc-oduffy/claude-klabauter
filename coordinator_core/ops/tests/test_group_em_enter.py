"""Tests for `groupem.enter` -- the composed MUTATING entry op.

Spec backlink: docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md § C5
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_enter as gee
from coordinator_core.op_scopes import OP_KEY_SCOPE


def test_payload_has_exactly_four_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-1")
    monkeypatch.setattr(
        gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: []
    )
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-1", "superseded_incumbent": None},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert set(result.keys()) == {"nomination", "roster", "digest", "baseline"}


def test_mutating_classification_registered():
    assert OP_CLASSIFICATION["groupem.enter"] is OpClass.MUTATING


def test_all_four_registration_points_resolve():
    assert "groupem.enter" in _registry_map.OP_MODULE_MAP
    assert _registry_map.OP_MODULE_MAP["groupem.enter"] == "coordinator_core.ops.group_em_enter"
    assert OP_KEY_SCOPE["groupem.enter"] == "none"
    assert OP_CLASSIFICATION["groupem.enter"] is OpClass.MUTATING
    assert ipc._REGISTRY.get("groupem.enter") is not None


def test_each_leg_degrades_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-2")
    monkeypatch.setattr(
        gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: []
    )
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )

    def _boom(*a, **k):
        raise RuntimeError("nomination boom")

    monkeypatch.setattr(gee.group_em_nomination, "claim", _boom)

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["nomination"] is None
    assert "nomination_error" in result
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": True}
    assert result["baseline"] == {
        "spawned": [],
        "exited": [],
        "changed": [],
        "first_tick": True,
    }


def test_roster_failure_degrades_digest_and_baseline_too(tmp_path, monkeypatch):
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-3")

    def _boom(*a, **k):
        raise RuntimeError("roster boom")

    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", _boom)
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-3", "superseded_incumbent": None},
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["roster"] is None
    assert "roster_error" in result
    assert result["digest"] is None
    assert result["digest_error"] == "roster-leg-failed"
    assert result["baseline"] is None
    assert result["baseline_error"] == "roster-leg-failed"
    assert result["nomination"]["claimed"] is True


def test_baseline_leg_writes_under_the_acted_on_repo_root_not_claude_klabauter(tmp_path, monkeypatch):
    """Regression for the P1: `_run_baseline` used to call `diff_and_persist`
    without a `repo_root`, so its default (`baseline._repo_root()` == the
    claude-klabauter checkout) swallowed every `groupem.enter` call against another
    repo. This test deliberately does NOT monkeypatch `diff_and_persist` --
    it exercises the real function, over a real `tmp_path` `repo_root`, and
    asserts the baseline file lands under THAT root."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-6")
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": False},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {"claimed": True, "holder": "caller-sid-6", "superseded_incumbent": None},
    )

    claude_klabauter_root = Path(__file__).resolve().parents[3]
    claude_klabauter_store_glob = list(
        (claude_klabauter_root / "state" / "subagent-share" / "caller-sid-6").glob(
            "group-em-baseline-*.json"
        )
    )
    assert not claude_klabauter_store_glob, "pre-existing stray fixture would corrupt this assertion"

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["baseline"] is not None
    assert result.get("baseline_error") is None
    assert result["baseline"]["first_tick"] is True

    target_dir = tmp_path / "state" / "subagent-share" / "caller-sid-6"
    written = list(target_dir.glob("group-em-baseline-*.json"))
    assert len(written) == 1, "baseline snapshot must land under the acted-on repo_root"

    claude_klabauter_store_after = list(
        (claude_klabauter_root / "state" / "subagent-share" / "caller-sid-6").glob(
            "group-em-baseline-*.json"
        )
    )
    assert not claude_klabauter_store_after, "baseline snapshot must NOT land under the claude-klabauter checkout"
