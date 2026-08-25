"""
Tests for coordinator_core.ops.review_mint.op ("review.mint_workflow").

Spec backlink: pln-the-review-skill-mints-its-own-26e933 § C3.
Pure op-boundary tests -- ``load_fragment()`` is monkeypatched to a
caller-injected fixture dict, exactly like C1/C2's own tests never touch
the sibling DoE-claude clone (see plan Anti-scope "Do not hardcode a
cross-repo absolute path"; C5's ``test_roundtrip.py`` is the sibling-clone
round trip, out of this file's scope).
"""

from __future__ import annotations

import re

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core import op_scopes
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.review_mint import op as review_mint_op
from coordinator_core.ops.review_mint.op import (
    PathEscapeError,
    ReviewTierUndeterminedError,
    _make_gate_policy,
    _review_mint_workflow,
)
from coordinator_core.ops.review_mint.roster import RosterFragmentError, Stage

_FRAGMENT = {
    "schema": "review-roster-fragment",
    "schema_version": 3,
    "blocking_verdicts": {
        "coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM",
        "coordinator:docs-checker": None,
    },
    "tiers": {
        "lightweight": {"stages": [{"agents": ["coordinator:code-reviewer"]}]},
        "standard": {
            "stages": [
                {"gate": True, "agents": ["coordinator:prior-art-checker"]},
                {"agents": ["coordinator:code-reviewer", "coordinator:staff-eng"]},
            ]
        },
        "full": {
            "stages": [
                {"gate": True, "agents": ["coordinator:prior-art-checker"]},
                {"agents": ["coordinator:code-reviewer"]},
            ]
        },
    },
}


def _write_plan_with_sizing(tmp_path, tshirt):
    sizing_dir = tmp_path / "state" / "sizings"
    sizing_dir.mkdir(parents=True)
    sizing_path = sizing_dir / "example.yaml"
    sizing_path.write_text(
        f"schema: sizing-object\nestimate:\n  tshirt: {tshirt}\n  provisional: false\n",
        encoding="utf-8",
    )

    plan_path = tmp_path / "example-plan.md"
    plan_path.write_text(
        "---\n"
        "title: \"Example\"\n"
        "sizing_object: \"state/sizings/example.yaml\"\n"
        "---\n\n# Example\n",
        encoding="utf-8",
    )
    return plan_path


def _write_plan_without_sizing(tmp_path):
    plan_path = tmp_path / "no-sizing-plan.md"
    plan_path.write_text(
        "---\ntitle: \"No sizing\"\nsizing_object: null\n---\n\n# No sizing\n",
        encoding="utf-8",
    )
    return plan_path


@pytest.fixture
def stub_load_fragment(monkeypatch):
    monkeypatch.setattr(review_mint_op, "load_fragment", lambda repo_root=None: _FRAGMENT)


# ---------------------------------------------------------------------------
# Registry resolution -- AC1
# ---------------------------------------------------------------------------


def test_review_mint_workflow_resolves_through_the_op_registry():
    assert "review.mint_workflow" in _REGISTRY
    assert _REGISTRY["review.mint_workflow"] is _review_mint_workflow


def test_review_mint_workflow_is_classified_mutating():
    assert "review.mint_workflow" in OP_CLASSIFICATION
    assert OP_CLASSIFICATION["review.mint_workflow"] is OpClass.MUTATING


def test_review_mint_workflow_is_scoped_none():
    assert op_scopes.OP_KEY_SCOPE["review.mint_workflow"] == "none"


# ---------------------------------------------------------------------------
# Required params and path guard
# ---------------------------------------------------------------------------


def test_review_mint_workflow_requires_plan_path(stub_load_fragment):
    with pytest.raises(ValueError, match="plan_path"):
        _review_mint_workflow({"output_path": "/tmp/whatever.mjs"})


def test_review_mint_workflow_requires_output_path(tmp_path, stub_load_fragment):
    plan_path = _write_plan_with_sizing(tmp_path, "M")
    with pytest.raises(ValueError, match="output_path"):
        _review_mint_workflow({"plan_path": str(plan_path)})


def test_review_mint_workflow_rejects_an_out_of_bounds_output_path(tmp_path, stub_load_fragment):
    plan_path = _write_plan_with_sizing(tmp_path, "M")
    contained_dir = tmp_path / "contained"
    contained_dir.mkdir()
    escaping_output = tmp_path / "outside" / "escaped.mjs"

    with pytest.raises(PathEscapeError):
        _review_mint_workflow(
            {
                "plan_path": str(plan_path),
                "output_path": str(escaping_output),
                "target_root": str(contained_dir),
            }
        )
    assert not escaping_output.exists()


# ---------------------------------------------------------------------------
# Round trip -- writes and returns the verdict, tier included
# ---------------------------------------------------------------------------


def test_review_mint_workflow_round_trip_writes_and_returns_verdict(tmp_path, stub_load_fragment):
    plan_path = _write_plan_with_sizing(tmp_path, "M")
    output_path = tmp_path / "out" / "review.mjs"
    output_path.parent.mkdir()

    result = _review_mint_workflow(
        {"plan_path": str(plan_path), "output_path": str(output_path)},
        repo_root=tmp_path,
    )

    assert result["path"] == str(output_path.resolve())
    assert output_path.is_file()
    written = output_path.read_text(encoding="utf-8")
    assert written
    assert result["ok"] is True
    assert result["error_count"] == 0
    assert isinstance(result["findings"], list)
    assert isinstance(result["warn_count"], int)
    assert result["tier"] == "standard"

    # Gate stage: schema-bearing, AC5-shaped abort branch present.
    assert "schema:" in written
    assert "blocking_agent" in written
    assert "BLOCKED-SURFACE-TO-PM" in written
    # No commit/pytest stage (AC9).
    assert "git commit" not in written
    assert "pytest" not in written
    # No model: key on a reviewer call (Anti-scope), but the doc/schema
    # scaffold still lists the phases in meta.phases.
    assert "model:" not in written


def test_review_mint_workflow_derives_lightweight_tier(tmp_path, stub_load_fragment):
    plan_path = _write_plan_with_sizing(tmp_path, "XS")
    output_path = tmp_path / "review.mjs"

    result = _review_mint_workflow(
        {"plan_path": str(plan_path), "output_path": str(output_path)},
        repo_root=tmp_path,
    )
    assert result["tier"] == "lightweight"


# ---------------------------------------------------------------------------
# Refusals propagate uncaught
# ---------------------------------------------------------------------------


def test_review_mint_workflow_raises_when_tier_undetermined(tmp_path, stub_load_fragment):
    plan_path = _write_plan_without_sizing(tmp_path)
    output_path = tmp_path / "review.mjs"

    with pytest.raises(ReviewTierUndeterminedError):
        _review_mint_workflow(
            {"plan_path": str(plan_path), "output_path": str(output_path)},
            repo_root=tmp_path,
        )
    assert not output_path.exists()


def test_review_mint_workflow_propagates_a_malformed_fragment(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review_mint_op, "load_fragment", lambda repo_root=None: {"schema": "review-roster-fragment"}
    )
    plan_path = _write_plan_with_sizing(tmp_path, "M")
    output_path = tmp_path / "review.mjs"

    with pytest.raises(RosterFragmentError):
        _review_mint_workflow(
            {"plan_path": str(plan_path), "output_path": str(output_path)},
            repo_root=tmp_path,
        )
    assert not output_path.exists()


# ---------------------------------------------------------------------------
# load_fragment() itself -- the sibling-clone-touching seam
# ---------------------------------------------------------------------------


def test_load_fragment_raises_when_doe_root_unresolved(monkeypatch):
    monkeypatch.setattr(review_mint_op, "read_doe_root_pointer", lambda: "")
    with pytest.raises(FileNotFoundError):
        review_mint_op.load_fragment()


def test_load_fragment_raises_when_fragment_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(review_mint_op, "read_doe_root_pointer", lambda: str(tmp_path))
    with pytest.raises(FileNotFoundError):
        review_mint_op.load_fragment()


def test_load_fragment_reads_and_parses_the_real_relpath(tmp_path, monkeypatch):
    fragment_dir = tmp_path / "coordinator" / "contract"
    fragment_dir.mkdir(parents=True)
    (fragment_dir / "review-roster-fragment.json").write_text(
        '{"schema": "review-roster-fragment", "tiers": {}}', encoding="utf-8"
    )
    monkeypatch.setattr(review_mint_op, "read_doe_root_pointer", lambda: str(tmp_path))
    fragment = review_mint_op.load_fragment()
    assert fragment == {"schema": "review-roster-fragment", "tiers": {}}


# ---------------------------------------------------------------------------
# AC12: gate freshness -- a stale/absent run_nonce refuses, before verdict
# ---------------------------------------------------------------------------
#
# `_make_gate_policy`'s branch text is JS, evaluated at Workflow runtime --
# not by this Python process. To genuinely exercise the nonce COMPARISON
# logic (not merely grep the emitted text for field names) this simulator
# parses the two sequential `if` blocks `_make_gate_policy` composes per
# blocking agent and replays their exact `!==`/`===` decisions against a
# fixture result dict, using a sentinel for "key absent" so a missing
# `run_nonce` reproduces JS's `undefined !== '<literal>'` (always true).

_MISSING = object()

_BRANCH_RE = re.compile(
    r"if \((\w+)\.(run_nonce|verdict) (!==|===) '([^']*)'\) \{\n"
    r"    return \{ ([^}]*) \};\n"
    r"  \}",
    re.S,
)


def _simulate_gate_branch(branch_text: str, result: dict):
    """Replay the composed if/if sequence against `result` (stands in for
    a captured JS object) and return which kind fired first: "refusal",
    "abort", or None if neither `if` matched -- mirrors evaluation order,
    since `_make_gate_policy` emits the nonce check strictly before the
    verdict check for every blocking agent."""
    for m in _BRANCH_RE.finditer(branch_text):
        _var, field, op_, literal, _body = m.groups()
        actual = result.get(field, _MISSING)
        matched = (actual != literal) if op_ == "!==" else (actual == literal)
        if matched:
            return "refusal" if field == "run_nonce" else "abort"
    return None


def test_gate_policy_refuses_on_a_different_nonce():
    policy = _make_gate_policy({"coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM"}, "issued-nonce")
    stage = Stage(agents=["coordinator:prior-art-checker"], gate=True)
    branch = policy(stage, 0, [("coordinator:prior-art-checker", "r")])
    result = {"run_nonce": "stale-nonce", "verdict": "OK"}
    assert _simulate_gate_branch(branch, result) == "refusal"


def test_gate_policy_refuses_on_an_absent_nonce():
    policy = _make_gate_policy({"coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM"}, "issued-nonce")
    stage = Stage(agents=["coordinator:prior-art-checker"], gate=True)
    branch = policy(stage, 0, [("coordinator:prior-art-checker", "r")])
    result = {"verdict": "OK"}  # no run_nonce field at all
    assert _simulate_gate_branch(branch, result) == "refusal"


def test_gate_policy_passes_through_to_verdict_check_on_a_matching_nonce():
    policy = _make_gate_policy({"coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM"}, "issued-nonce")
    stage = Stage(agents=["coordinator:prior-art-checker"], gate=True)
    branch = policy(stage, 0, [("coordinator:prior-art-checker", "r")])

    matching_clean = {"run_nonce": "issued-nonce", "verdict": "OK"}
    assert _simulate_gate_branch(branch, matching_clean) is None

    matching_blocking = {"run_nonce": "issued-nonce", "verdict": "BLOCKED-SURFACE-TO-PM"}
    assert _simulate_gate_branch(branch, matching_blocking) == "abort"


def test_stale_nonce_with_a_blocking_verdict_yields_refusal_not_abort():
    """The regression that matters: a stale sidecar carrying a real
    BLOCKED-SURFACE-TO-PM verdict must not launder into AC5's abort shape --
    the nonce check runs first and wins."""
    policy = _make_gate_policy({"coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM"}, "issued-nonce")
    stage = Stage(agents=["coordinator:prior-art-checker"], gate=True)
    branch = policy(stage, 0, [("coordinator:prior-art-checker", "r")])

    stale_but_blocking = {"run_nonce": "stale-nonce", "verdict": "BLOCKED-SURFACE-TO-PM"}
    assert _simulate_gate_branch(branch, stale_but_blocking) == "refusal"

    # Shape check on the emitted text itself: the refusal branch is
    # distinguishable from the abort branch and names what happened.
    assert "gate_refused: 'stale-or-missing-run-nonce'" in branch
    assert "expected_nonce:" in branch and "received_nonce:" in branch


def test_gate_policy_disarmed_for_a_non_blocking_agent_regardless_of_nonce():
    """AC6, unaffected by AC12: an agent absent from blocking_verdicts (or
    mapped to null) contributes no branch at all -- no nonce check either,
    since there is no verdict to protect."""
    policy = _make_gate_policy({"coordinator:docs-checker": None}, "issued-nonce")
    stage = Stage(agents=["coordinator:docs-checker"], gate=True)
    branch = policy(stage, 0, [("coordinator:docs-checker", "r")])
    assert branch == ""


# ---------------------------------------------------------------------------
# Multiple blocking-capable agents in one gate stage -- evaluation order
# (code-review s4 WARN: no fixture before this exercised 2+ agents that both
# carry a non-null blocking_verdicts entry; v3's `full` tier roster had one,
# v4 dropped it, so the gap was latent rather than live).
# ---------------------------------------------------------------------------


def _simulate_gate_branch_multi(branch_text: str, results_by_var: dict):
    """Like `_simulate_gate_branch`, but keyed by the captured var name so a
    multi-agent branch (one `if`/`if` pair per blocking agent, concatenated
    in `results` order) can be replayed against a DIFFERENT captured result
    per agent. Returns `(kind, var)` for the first `if` that matches, or
    `(None, None)` if none does -- the var tells the test which agent's
    branch actually fired, so evaluation order is asserted directly rather
    than merely inferred from the outcome."""
    for m in _BRANCH_RE.finditer(branch_text):
        var, field, op_, literal, _body = m.groups()
        result = results_by_var.get(var, {})
        actual = result.get(field, _MISSING)
        matched = (actual != literal) if op_ == "!==" else (actual == literal)
        if matched:
            return ("refusal" if field == "run_nonce" else "abort"), var
    return None, None


def test_gate_policy_multiple_blocking_agents_second_agent_stale_nonce_still_refuses():
    """Two blocking-capable agents in one stage: agent1's nonce is fresh and
    its verdict is clean (no match at all), agent2's nonce is stale. First-
    match-wins, evaluated in `results` order (agent1's pair, then agent2's),
    must still reach and refuse on agent2's stale nonce -- agent1 producing
    no match must not short-circuit the stage as a pass."""
    policy = _make_gate_policy(
        {
            "coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM",
            "coordinator:docs-checker": "BLOCKED",
        },
        "issued-nonce",
    )
    stage = Stage(
        agents=["coordinator:prior-art-checker", "coordinator:docs-checker"], gate=True
    )
    branch = policy(
        stage,
        0,
        [("coordinator:prior-art-checker", "r1"), ("coordinator:docs-checker", "r2")],
    )
    results_by_var = {
        "r1": {"run_nonce": "issued-nonce", "verdict": "OK"},
        "r2": {"run_nonce": "stale-nonce", "verdict": "OK"},
    }
    assert _simulate_gate_branch_multi(branch, results_by_var) == ("refusal", "r2")


def test_gate_policy_multiple_blocking_agents_first_agent_stale_nonce_refuses_before_second_checked():
    """Converse ordering: agent1's nonce is stale, agent2's is fresh and
    clean. The refusal must fire on agent1's pair -- first in evaluation
    order -- before agent2's pair is ever reached."""
    policy = _make_gate_policy(
        {
            "coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM",
            "coordinator:docs-checker": "BLOCKED",
        },
        "issued-nonce",
    )
    stage = Stage(
        agents=["coordinator:prior-art-checker", "coordinator:docs-checker"], gate=True
    )
    branch = policy(
        stage,
        0,
        [("coordinator:prior-art-checker", "r1"), ("coordinator:docs-checker", "r2")],
    )
    results_by_var = {
        "r1": {"run_nonce": "stale-nonce", "verdict": "OK"},
        "r2": {"run_nonce": "issued-nonce", "verdict": "OK"},
    }
    assert _simulate_gate_branch_multi(branch, results_by_var) == ("refusal", "r1")


def test_review_mint_workflow_returns_the_minted_run_nonce(tmp_path, stub_load_fragment):
    plan_path = _write_plan_with_sizing(tmp_path, "M")
    output_path = tmp_path / "review.mjs"

    result = _review_mint_workflow(
        {"plan_path": str(plan_path), "output_path": str(output_path)},
        repo_root=tmp_path,
    )
    assert isinstance(result["run_nonce"], str) and result["run_nonce"]
    written = output_path.read_text(encoding="utf-8")
    assert result["run_nonce"] in written
    assert "gate_refused" in written


def test_review_mint_workflow_accepts_a_pinned_run_nonce(tmp_path, stub_load_fragment):
    plan_path = _write_plan_with_sizing(tmp_path, "M")
    output_path = tmp_path / "review.mjs"

    result = _review_mint_workflow(
        {
            "plan_path": str(plan_path),
            "output_path": str(output_path),
            "run_nonce": "pinned-for-test",
        },
        repo_root=tmp_path,
    )
    assert result["run_nonce"] == "pinned-for-test"
    written = output_path.read_text(encoding="utf-8")
    assert "pinned-for-test" in written


def test_review_mint_workflow_mints_a_fresh_nonce_each_call_by_default(tmp_path, stub_load_fragment):
    plan_path = _write_plan_with_sizing(tmp_path, "M")

    first = _review_mint_workflow(
        {"plan_path": str(plan_path), "output_path": str(tmp_path / "a.mjs")},
        repo_root=tmp_path,
    )
    second = _review_mint_workflow(
        {"plan_path": str(plan_path), "output_path": str(tmp_path / "b.mjs")},
        repo_root=tmp_path,
    )
    assert first["run_nonce"] != second["run_nonce"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
