"""Tests for grind_profile.py (docs/plans/2026-09-21-bug-blitz-emitter-
engine-leg.md Tasks § C3): load, name refusal, unknown key, appetite
resolution and its concurrency clamp, overrides, one graph-floor refusal per
rule, and the EM-authored STAGE_OUTCOMES totality check."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from coordinator_core.contract import grind_vocab as vocab
from coordinator_core.ops.dispatch_emit import grind_profile as gp

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


def _load_fixture_doc() -> dict:
    text = (_FIXTURE_DIR / "fixture.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _write_profile(tmp_path: Path, name: str, doc: dict) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _profile_from_doc(tmp_path: Path, name: str, doc: dict) -> gp.Profile:
    _write_profile(tmp_path, name, doc)
    return gp.load_profile(name, tmp_path)


# ---------------------------------------------------------------------------
# load_profile
# ---------------------------------------------------------------------------


def test_load_profile_fixture_round_trip():
    profile = gp.load_profile("fixture", _FIXTURE_DIR)
    assert profile.name == "fixture"
    assert profile.row_id_key == "@stem"
    assert profile.batch_key == ("severity",)
    assert profile.verdicts == ("confirmed-bug", "not-reproduced")
    assert set(profile.graph) == {"triage", "refute_close", "fix", "verify", "commit"}
    assert profile.triage_policy_sha256 == hashlib.sha256(
        profile.triage_policy.encode("utf-8")
    ).hexdigest()


def test_load_profile_name_refusal():
    for bad_name in ("Fixture", "../fixture", "a/b"):
        with pytest.raises(gp.ProfileError) as exc_info:
            gp.load_profile(bad_name, _FIXTURE_DIR)
        assert exc_info.value.rule == "invalid_profile_name"


def test_load_profile_unknown_top_level_key(tmp_path):
    doc = _load_fixture_doc()
    doc["not-a-real-field"] = True
    _write_profile(tmp_path, "bad-profile", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.load_profile("bad-profile", tmp_path)
    assert exc_info.value.rule == "unknown_profile_key"


def test_load_profile_fixture_passes_totality():
    profile = gp.load_profile("fixture", _FIXTURE_DIR)
    gp.validate_graph(profile)  # must not raise


# ---------------------------------------------------------------------------
# resolve_appetite
# ---------------------------------------------------------------------------


def test_resolve_appetite_basic():
    profile = gp.load_profile("fixture", _FIXTURE_DIR)
    resolved = gp.resolve_appetite(profile, "standard")
    assert resolved["concurrency"] == 4
    assert resolved["window"] == 2
    assert resolved["max_agent_calls"] == 40


def test_resolve_appetite_concurrency_clamp():
    profile = gp.load_profile("fixture", _FIXTURE_DIR)
    resolved = gp.resolve_appetite(profile, "sweep")
    assert resolved["concurrency"] == vocab.ENGINE_CONCURRENCY_CEILING
    assert resolved["concurrency"] < 16


def test_resolve_appetite_overrides():
    profile = gp.load_profile("fixture", _FIXTURE_DIR)
    resolved = gp.resolve_appetite(
        profile, "standard", overrides={"limit": 10, "where": [["severity", "==", "P0"]]}
    )
    assert resolved["limit"] == 10
    assert resolved["where"] == [["severity", "==", "P0"]]
    # the base profile preset is untouched by mutation of the resolved copy
    assert profile.appetite["standard"]["limit"] is None


def test_resolve_appetite_rejects_non_overridable_knob():
    profile = gp.load_profile("fixture", _FIXTURE_DIR)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.resolve_appetite(profile, "standard", overrides={"concurrency": 1})
    assert exc_info.value.rule == "unoverridable_knob"


def test_resolve_appetite_unknown_preset():
    profile = gp.load_profile("fixture", _FIXTURE_DIR)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.resolve_appetite(profile, "hunt")
    assert exc_info.value.rule == "unknown_appetite_preset"


# ---------------------------------------------------------------------------
# validate_graph — one refusal per floor rule, plus totality
# ---------------------------------------------------------------------------

_CLOSURE = {
    "status_field": "status",
    "closed_values": {"fix": "fixed", "refute-close": "not-a-bug"},
    "stamp_fields": ["closed_at", "closed_by"],
}

_APPETITE = {
    "standard": {
        "concurrency": 4,
        "extra_verification": False,
        "batch_size": {"default": 4},
        "triage_depth": {"default": "standard"},
        "window": 2,
        "max_agent_calls": 40,
    }
}


def _minimal_doc(graph: dict, *, verdicts=("confirmed-bug", "not-reproduced")) -> dict:
    return {
        "row_id_key": "@stem",
        "batch_key": ["severity"],
        "priority": {"field": "severity", "order": "asc"},
        "verdicts": list(verdicts),
        "graph": graph,
        "closure": _CLOSURE,
        "triage_policy": "Triage per the manifest.",
        "appetite": _APPETITE,
        "archive_path": "state/bug-backlog/archive",
        "schema": "coordinator_core/frontmatter/schemas/bug-backlog.schema.json",
    }


def test_validate_graph_verify_then_fix_refused(tmp_path):
    # fix's `done` edge goes straight to commit, skipping verify entirely.
    doc = _minimal_doc(
        {
            "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "refute_close"}},
            "refute_close": {"kind": "refute-close", "edges": {"confirmed": "commit", "refuted": "commit"}},
            "fix": {
                "kind": "fix",
                "edges": {
                    "done": "commit",
                    "NEEDS_WIDER_SCOPE": "widen-exhausted",
                    "PEER_DIRTY": "peer-dirty",
                    "NOT_REPRODUCED": "verify-failed",
                    "baton": "baton",
                    "needs-judgment": "needs-judgment",
                },
            },
            "commit": {"kind": "commit", "edges": {}},
        }
    )
    profile = _profile_from_doc(tmp_path, "verify-then-fix", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "verify_floor"


def test_validate_graph_closing_path_without_refute_close_refused(tmp_path):
    # triage routes not-reproduced straight to commit, bypassing refute-close.
    doc = _minimal_doc(
        {
            "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "commit"}},
            "fix": {
                "kind": "fix",
                "edges": {
                    "done": "verify",
                    "NEEDS_WIDER_SCOPE": "widen-exhausted",
                    "PEER_DIRTY": "peer-dirty",
                    "NOT_REPRODUCED": "verify",
                    "baton": "baton",
                    "needs-judgment": "needs-judgment",
                },
            },
            "verify": {
                "kind": "verify",
                "verify": {"default": "agent"},
                "edges": {"pass": "commit", "fail": "verify-failed"},
            },
            "commit": {"kind": "commit", "edges": {}},
        }
    )
    profile = _profile_from_doc(tmp_path, "no-refute-close", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "closing_floor"


def test_validate_graph_non_on_fail_cycle_refused(tmp_path):
    # a plain `edges` cycle: fix -> verify -> fix (not via on_fail).
    doc = _minimal_doc(
        {
            "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "refute_close"}},
            "refute_close": {"kind": "refute-close", "edges": {"confirmed": "commit", "refuted": "commit"}},
            "fix": {
                "kind": "fix",
                "edges": {
                    "done": "verify",
                    "NEEDS_WIDER_SCOPE": "widen-exhausted",
                    "PEER_DIRTY": "peer-dirty",
                    "NOT_REPRODUCED": "verify",
                    "baton": "baton",
                    "needs-judgment": "needs-judgment",
                },
            },
            "verify": {
                "kind": "verify",
                "verify": {"default": "agent"},
                "edges": {"pass": "commit", "fail": "fix"},
            },
            "commit": {"kind": "commit", "edges": {}},
        }
    )
    profile = _profile_from_doc(tmp_path, "edge-cycle", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "graph_cycle"


def test_validate_graph_second_on_fail_traversal_refused(tmp_path):
    # verify's on_fail retries fix; fix's own on_fail retries verify, so one
    # path can chain two DISTINCT on_fail hops before reaching commit.
    doc = _minimal_doc(
        {
            "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "refute_close"}},
            "refute_close": {"kind": "refute-close", "edges": {"confirmed": "commit", "refuted": "commit"}},
            "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": "verify"},
            "verify": {
                "kind": "verify",
                "verify": {"default": "agent"},
                "edges": {"pass": "commit"},
                "on_fail": "fix",
            },
            "commit": {"kind": "commit", "edges": {}},
        }
    )
    profile = _profile_from_doc(tmp_path, "double-on-fail", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "graph_on_fail_traversal"


def test_validate_graph_stray_outcome_outside_stage_outcomes_refused(tmp_path):
    doc = _minimal_doc(
        {
            "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "refute_close"}},
            "refute_close": {"kind": "refute-close", "edges": {"confirmed": "commit", "refuted": "commit"}},
            "fix": {"kind": "fix", "edges": {"done": "verify", "not-a-real-outcome": "commit"}},
            "verify": {"kind": "verify", "verify": {"default": "agent"}, "edges": {"pass": "commit"}},
            "commit": {"kind": "commit", "edges": {}},
        }
    )
    profile = _profile_from_doc(tmp_path, "stray-outcome", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "stray_outcome"


def test_validate_graph_triage_totality_refused_when_incomplete(tmp_path):
    doc = _minimal_doc(
        {
            "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix"}},  # missing not-reproduced
            "fix": {"kind": "fix", "edges": {"done": "verify"}},
            "verify": {"kind": "verify", "verify": {"default": "agent"}, "edges": {"pass": "commit"}},
            "commit": {"kind": "commit", "edges": {}},
        }
    )
    profile = _profile_from_doc(tmp_path, "not-total", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "triage_verdict_totality"


def test_validate_graph_refute_close_verify_block_refused(tmp_path):
    # refute-close is agent-only: caught at load time, before validate_graph
    # ever runs, since a verify block on that node kind is never a legal
    # profile shape in the first place.
    doc = _load_fixture_doc()
    doc["graph"]["refute_close"]["verify"] = {"default": "agent"}
    _write_profile(tmp_path, "refute-close-verify", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.load_profile("refute-close-verify", tmp_path)
    assert exc_info.value.rule == "refute_close_not_agent_only"


def test_validate_graph_verify_default_missing_refused(tmp_path):
    doc = _load_fixture_doc()
    doc["graph"]["verify"]["verify"] = {"P0": {"mode": "op", "op": "lessons.verify_extraction"}}
    profile = _profile_from_doc(tmp_path, "no-default", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "verify_default_missing"


def test_validate_graph_verify_op_unknown_refused(tmp_path):
    doc = _load_fixture_doc()
    doc["graph"]["verify"]["verify"]["P0"] = {"mode": "op", "op": "not-a-real-op"}
    profile = _profile_from_doc(tmp_path, "bad-op", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "verify_op_unknown"


def test_validate_graph_closure_branch_missing_refused(tmp_path):
    doc = _load_fixture_doc()
    del doc["closure"]["closed_values"]["refute-close"]
    profile = _profile_from_doc(tmp_path, "missing-branch", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "closure_branch_missing"


def test_validate_graph_unknown_edge_target_refused(tmp_path):
    doc = _load_fixture_doc()
    doc["graph"]["fix"]["edges"]["done"] = "not-a-node-or-handback"
    profile = _profile_from_doc(tmp_path, "bad-target", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "unknown_edge_target"


# ---------------------------------------------------------------------------
# Profile hand-back types (DR-404 § 3) and on_fail to a hand-back type
# ---------------------------------------------------------------------------


def _handback_graph(*, fix_on_fail: str) -> dict:
    return {
        "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "park"}},
        "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": fix_on_fail},
        "verify": {
            "kind": "verify",
            "verify": {"default": "agent"},
            "edges": {"pass": "commit"},
            "on_fail": "needs-judgment",
        },
        "commit": {"kind": "commit", "edges": {}, "on_fail": "commit-failed"},
    }


def test_profile_hand_back_type_is_a_legal_edge_target(tmp_path):
    doc = _minimal_doc(_handback_graph(fix_on_fail="needs-judgment"))
    doc["hand_back_types"] = ["park", "wont-do"]
    profile = _profile_from_doc(tmp_path, "profile-handback", doc)
    assert profile.hand_back_types == ("park", "wont-do")
    assert "park" in profile.all_hand_back_types
    assert "budget-exhausted" in profile.all_hand_back_types
    gp.validate_graph(profile)


def test_undeclared_profile_hand_back_type_refused(tmp_path):
    doc = _minimal_doc(_handback_graph(fix_on_fail="needs-judgment"))
    profile = _profile_from_doc(tmp_path, "undeclared-handback", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "unknown_edge_target"


def test_on_fail_may_target_a_hand_back_type(tmp_path):
    doc = _minimal_doc(_handback_graph(fix_on_fail="park"))
    doc["hand_back_types"] = ["park"]
    gp.validate_graph(_profile_from_doc(tmp_path, "on-fail-handback", doc))


def test_on_fail_to_unknown_target_still_refused(tmp_path):
    doc = _minimal_doc(_handback_graph(fix_on_fail="nowhere"))
    doc["hand_back_types"] = ["park"]
    profile = _profile_from_doc(tmp_path, "on-fail-nowhere", doc)
    with pytest.raises(gp.ProfileError) as exc_info:
        gp.validate_graph(profile)
    assert exc_info.value.rule == "unknown_edge_target"


@pytest.mark.parametrize("collides", ["stage-dead", "verify"])
def test_hand_back_types_must_be_disjoint_from_universal_and_nodes(tmp_path, collides):
    doc = _minimal_doc(_handback_graph(fix_on_fail="needs-judgment"))
    doc["hand_back_types"] = ["park", collides]
    with pytest.raises(gp.ProfileError) as exc_info:
        _profile_from_doc(tmp_path, "collision", doc)
    assert exc_info.value.rule == "hand_back_type_collision"


@pytest.mark.parametrize("bad", [["Park"], ["park", "park"], "park", [3]])
def test_hand_back_types_shape_refused(tmp_path, bad):
    doc = _minimal_doc(_handback_graph(fix_on_fail="needs-judgment"))
    doc["hand_back_types"] = bad
    with pytest.raises(gp.ProfileError) as exc_info:
        _profile_from_doc(tmp_path, "bad-shape", doc)
    assert exc_info.value.rule == "profile_field_type"
