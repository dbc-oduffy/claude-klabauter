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


def test_live_incumbent_refusal_stops_before_digest(tmp_path, monkeypatch):
    """The load-bearing assertion: a REFUSED crown (live incumbent) must never call
    `build_send_digest` -- the bug is the side effect (cooldown arming), not the return
    shape. Spy on the digest builder rather than only checking its absence in the
    payload."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-7")

    roster_spy_calls: list = []
    digest_spy_calls: list = []
    baseline_spy_calls: list = []

    monkeypatch.setattr(
        gee.group_em_read_pass,
        "build_candidate_roster",
        lambda *a, **k: roster_spy_calls.append((a, k)) or [],
    )
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: digest_spy_calls.append((a, k)) or {"entries": []},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: baseline_spy_calls.append((a, k)) or {"first_tick": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": False,
            "holder": "incumbent-sid",
            "already_held": False,
            "superseded_incumbent": {
                "session_id": "incumbent-sid",
                "peer_name": "peer",
                "nominated_at": "2026-08-30T00:00:00Z",
                "nominated_by": "someone",
                "live": True,
                "live_reason": "live",
            },
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert digest_spy_calls == [], "build_send_digest must NEVER be called on a refused crown"
    assert roster_spy_calls == [], "roster leg must not be built on a refused crown either"
    assert baseline_spy_calls == []

    assert result["nomination"]["claimed"] is False
    assert result["nomination"]["already_held"] is False
    assert result["nomination"]["superseded_incumbent"]["live"] is True
    assert result["nomination"]["superseded_incumbent"]["live_reason"] == "live"

    # ABSENT, not null: an empty roster is a fact ("looked, found nobody"); an
    # absent one means "had no standing to look". Assert via `not in`, never
    # via `is None`, or this regresses to the exact bug the constraint exists
    # to prevent.
    assert "roster" not in result
    assert "digest" not in result
    assert "baseline" not in result
    assert "roster_error" not in result
    assert "digest_error" not in result
    assert "baseline_error" not in result


def test_unaccounted_incumbent_refusal_also_stops_before_digest(tmp_path, monkeypatch):
    """An incumbent with only ABSENCE of registry evidence (`no_registry_record`) is
    still a refusal -- this fleet is multi-machine, so no registry row for that
    session_id does not mean it is gone, only that we cannot see it from here. Same
    refusal shape as a live incumbent: roster/digest/baseline absent, no digest call.
    (Contrast `test_pid_not_running_incumbent_is_auto_replaced_not_refused` below --
    `pid_not_running` is POSITIVE evidence of death and is NOT this case.)"""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-8")

    digest_spy_calls: list = []
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: digest_spy_calls.append((a, k)) or {"entries": []},
    )
    monkeypatch.setattr(
        gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: []
    )
    monkeypatch.setattr(
        gee.group_em_baseline, "diff_and_persist", lambda *a, **k: {"first_tick": True}
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": False,
            "holder": "unaccounted-incumbent-sid",
            "already_held": False,
            "superseded_incumbent": {
                "session_id": "unaccounted-incumbent-sid",
                "peer_name": "peer",
                "nominated_at": "2026-08-29T00:00:00Z",
                "nominated_by": "someone",
                "live": False,
                "live_reason": "no_registry_record",
            },
            "replaced_holder": None,
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert digest_spy_calls == [], "absence of registry evidence must not auto-yield the crown"
    assert result["nomination"]["claimed"] is False
    assert result["nomination"]["already_held"] is False
    assert result["nomination"]["superseded_incumbent"]["live"] is False
    assert result["nomination"]["superseded_incumbent"]["live_reason"] == "no_registry_record"
    assert result["nomination"]["replaced_holder"] is None
    assert "roster" not in result
    assert "digest" not in result
    assert "baseline" not in result
    assert "roster_error" not in result
    assert "digest_error" not in result
    assert "baseline_error" not in result


def test_pid_not_running_incumbent_is_auto_replaced_not_refused(tmp_path, monkeypatch):
    """AUTO-REPLACE: `live_reason == "pid_not_running"` is POSITIVE evidence of death
    (a registry row exists for the incumbent and its pid is confirmed not running).
    `claimed` is True, roster/digest/baseline all run, and the replaced holder is named
    in its OWN `replaced_holder` field -- never folded into `superseded_incumbent`,
    whose presence would wrongly signal a refusal. A test that only checked
    `claimed is True` would pass a silent replacement; assert `replaced_holder` is
    populated and distinct from `superseded_incumbent` (which must be None here)."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-11")

    digest_spy_calls: list = []
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: digest_spy_calls.append((a, k))
        or {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": False},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": True,
            "holder": "caller-sid-11",
            "already_held": False,
            "superseded_incumbent": None,
            "replaced_holder": {
                "session_id": "dead-incumbent-sid",
                "peer_name": "peer",
                "nominated_at": "2026-08-29T00:00:00Z",
                "nominated_by": "someone",
                "live": False,
                "live_reason": "pid_not_running",
            },
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["nomination"]["claimed"] is True
    assert result["nomination"]["already_held"] is False
    assert result["nomination"]["superseded_incumbent"] is None
    replaced = result["nomination"]["replaced_holder"]
    assert replaced is not None, "a silent replacement must not pass this test"
    assert replaced != result["nomination"]["superseded_incumbent"]
    assert replaced["session_id"] == "dead-incumbent-sid"
    assert replaced["live_reason"] == "pid_not_running"

    # crown was successfully claimed -- roster/digest/baseline all run, keys present.
    assert digest_spy_calls != [], "an auto-replace must proceed to build the digest"
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": True}
    assert result["baseline"]["first_tick"] is False
    assert "roster_error" not in result
    assert "digest_error" not in result
    assert "baseline_error" not in result


def test_successful_claim_still_returns_all_four_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-9")
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
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
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": True,
            "holder": "caller-sid-9",
            "already_held": False,
            "superseded_incumbent": None,
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert set(result.keys()) >= {"nomination", "roster", "digest", "baseline"}
    assert result["nomination"]["claimed"] is True
    assert result["nomination"]["already_held"] is False
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": True}
    assert result["baseline"]["first_tick"] is True


def test_reentry_by_holder_is_distinguishable_from_fresh_claim(tmp_path, monkeypatch):
    """CONSTRAINT A: `already_held` must be surfaced verbatim, not collapsed. A fresh
    claim and a refreshed re-entry by the same holder are two different lines to a
    human operator and must be distinguishable from the payload alone."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-10")
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": True},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": False},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": True,
            "holder": "caller-sid-10",
            "already_held": True,
            "superseded_incumbent": None,
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["nomination"]["claimed"] is True
    assert result["nomination"]["already_held"] is True
    assert result["nomination"]["superseded_incumbent"] is None
    assert result["roster"] == []


def test_auto_replace_crown_is_not_a_refusal_and_runs_roster(tmp_path, monkeypatch):
    """`replaced_holder` (case 4 -- pid_not_running) is NOT a refusal: `claimed` is True,
    so roster/digest/baseline must all run, unlike the two refusal cases above."""
    monkeypatch.setattr(gee.group_em_read_pass, "caller_session_id", lambda: "caller-sid-11")
    monkeypatch.setattr(gee.group_em_read_pass, "build_candidate_roster", lambda *a, **k: [])
    monkeypatch.setattr(
        gee.group_em_send_pass,
        "build_send_digest",
        lambda *a, **k: {"entries": [], "gate_declaration_required": False},
    )
    monkeypatch.setattr(
        gee.group_em_baseline,
        "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )
    monkeypatch.setattr(
        gee.group_em_nomination,
        "claim",
        lambda *a, **k: {
            "claimed": True,
            "holder": "caller-sid-11",
            "already_held": False,
            "superseded_incumbent": None,
            "replaced_holder": {
                "session_id": "dead-incumbent-sid",
                "peer_name": "peer",
                "nominated_at": "2026-08-29T00:00:00Z",
                "nominated_by": "someone",
                "live": False,
                "live_reason": "pid_not_running",
            },
        },
    )

    result = gee._group_em_enter({"repo_root": str(tmp_path)})

    assert result["nomination"]["claimed"] is True
    assert result["nomination"]["already_held"] is False
    assert result["nomination"]["replaced_holder"]["session_id"] == "dead-incumbent-sid"
    assert result["nomination"]["replaced_holder"]["live_reason"] == "pid_not_running"
    # Not a refusal -- roster/digest/baseline all ran, none absent.
    assert result["roster"] == []
    assert result["digest"] == {"entries": [], "gate_declaration_required": False}
    assert result["baseline"]["first_tick"] is True


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
