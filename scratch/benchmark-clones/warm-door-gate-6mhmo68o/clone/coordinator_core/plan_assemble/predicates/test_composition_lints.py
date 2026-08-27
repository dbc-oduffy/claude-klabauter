"""
Tests for coordinator_core.plan_assemble.predicates.composition_lints —
the seven Layer 0 Branch C (a) leaf readers.

Purpose: proves each of `:136`, `:137`, `:143`, `:150`, `:152`, `:153`,
`:172` resolves to either a populated field (matching the shape its own
function docstring names) or `predicates.undetermined(...)`, never a bare
`False`/`None`/silent absence — the Executor hard constraints' fallback
rule (docs/plans/2026-08-13-plan-assemble-wave-2-the-predicate-producers.md
§ Executor hard constraints).

Negative-spec:
  - Does NOT test `residue.py`'s `gates` assembly — that wiring is C13's
    exclusive write target and out of this chunk's scope.
  - Does NOT test any `U`-classified judgment arm (there is none in this
    module's rows) — AC4 has no surface here to prove.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C5
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates.composition_lints import (
    ac_reject_list,
    chunk_index_sidecar,
    concurrency_shared_state,
    deferral_case_against,
    hard_constraints_block,
    spine_row_shape,
    stub_spawns_subagents,
)


def _ctx(*, plan_body: str | None = None, repo_root: Path | None = None) -> PredicateContext:
    return PredicateContext(
        repo_root=repo_root if repo_root is not None else Path("/tmp/nonexistent-repo-root"),
        plan_path=Path("/tmp/plan.md") if plan_body is not None else None,
        plan_frontmatter={} if plan_body is not None else None,
        plan_body=plan_body,
        sizing_object_path=None,
        sizing_frontmatter=None,
        resolved_route="spec-dispatch",
        caller_flags={},
    )


_SPINE_HEADING = "## Tasks\n\n"


def _spine(body_yaml: str) -> str:
    return f"{_SPINE_HEADING}```yaml plan-tasks\n{body_yaml}\n```\n"


# ---------------------------------------------------------------------------
# :136 spine_row_shape


def test_spine_row_shape_no_plan_is_undetermined():
    result = spine_row_shape(_ctx(plan_body=None))
    assert result["undetermined"] is True
    assert "reason" in result


def test_spine_row_shape_absent_fence_is_vacuously_valid():
    result = spine_row_shape(_ctx(plan_body="## Tasks\n\nno fence here.\n"))
    assert result == {"valid": True}


def test_spine_row_shape_valid_open_row():
    body = _spine("- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n  "
                   "queue_scope: project\n  disposition: open\n  body: |\n    x\n")
    result = spine_row_shape(_ctx(plan_body=body))
    assert result == {"valid": True}


def test_spine_row_shape_invalid_missing_case_against():
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n  "
        "queue_scope: project\n  disposition: backlogged\n  disposition_detail: cut\n  "
        "body: |\n    x\n"
    )
    result = spine_row_shape(_ctx(plan_body=body))
    assert result == {"valid": False}


def test_spine_row_shape_pm_approval_arm_excluded():
    # backlogged with case_against but no pm_approved must still pass shape —
    # the U-classified pm_approved leg is excluded via governed=True.
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n  "
        "queue_scope: project\n  disposition: backlogged\n  disposition_detail: cut\n  "
        "disposition_ref: docs/backlog.md\n  "
        "case_against: it costs too much\n  body: |\n    x\n"
    )
    result = spine_row_shape(_ctx(plan_body=body))
    assert result == {"valid": True}


# ---------------------------------------------------------------------------
# :137 ac_reject_list


def test_ac_reject_list_no_plan_is_undetermined():
    result = ac_reject_list(_ctx(plan_body=None))
    assert result["undetermined"] is True


def test_ac_reject_list_flags_vague_qualifier():
    body = "| AC1 | The feature works properly. | open |\n"
    result = ac_reject_list(_ctx(plan_body=body))
    assert result["hits"] == [{"ac_id": "AC1", "matched_pattern": "vague_qualifier"}]


def test_ac_reject_list_clean_ac_has_no_hits():
    body = "| AC1 | `foo()` returns 42 for input 0. | open |\n"
    result = ac_reject_list(_ctx(plan_body=body))
    assert result["hits"] == []


def test_ac_reject_list_inline_style_fallback():
    body = "AC1: this should work fine in most cases.\n"
    result = ac_reject_list(_ctx(plan_body=body))
    assert {"ac_id": "AC1", "matched_pattern": "hedge_should_work"} in result["hits"]


# ---------------------------------------------------------------------------
# :143 deferral_case_against


def test_deferral_case_against_no_plan_is_undetermined():
    result = deferral_case_against(_ctx(plan_body=None))
    assert result["undetermined"] is True


def test_deferral_case_against_present_and_nonvacuous():
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n  "
        "queue_scope: project\n  disposition: backlogged\n  disposition_detail: cut\n  "
        "case_against: real reasoning here\n  body: |\n    x\n"
    )
    result = deferral_case_against(_ctx(plan_body=body))
    assert result["entries"] == [{
        "id": "C1",
        "case_against_present": True,
        "case_against_text": "real reasoning here",
    }]


def test_deferral_case_against_absent_reports_false():
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n  "
        "queue_scope: project\n  disposition: wont_do\n  disposition_detail: cut\n  "
        "body: |\n    x\n"
    )
    result = deferral_case_against(_ctx(plan_body=body))
    assert result["entries"] == [{
        "id": "C1",
        "case_against_present": False,
        "case_against_text": None,
    }]


def test_deferral_case_against_excludes_non_deferral_dispositions():
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n  "
        "queue_scope: project\n  disposition: coded\n  disposition_ref: abc1234\n  "
        "body: |\n    x\n"
    )
    result = deferral_case_against(_ctx(plan_body=body))
    assert result["entries"] == []


# ---------------------------------------------------------------------------
# :150 hard_constraints_block


def test_hard_constraints_block_no_plan_is_undetermined():
    result = hard_constraints_block(_ctx(plan_body=None))
    assert result["undetermined"] is True


def test_hard_constraints_block_present():
    result = hard_constraints_block(_ctx(plan_body="## Executor hard constraints\n\ntext\n"))
    assert result == {"present": True}


def test_hard_constraints_block_absent():
    result = hard_constraints_block(_ctx(plan_body="## Problem\n\ntext\n"))
    assert result == {"present": False}


# ---------------------------------------------------------------------------
# :152 stub_spawns_subagents


def test_stub_spawns_subagents_no_plan_is_undetermined():
    result = stub_spawns_subagents(_ctx(plan_body=None))
    assert isinstance(result, dict) and result["undetermined"] is True


def test_stub_spawns_subagents_absent_fence_is_false():
    result = stub_spawns_subagents(_ctx(plan_body="## Tasks\n\nno fence.\n"))
    assert result is False


def test_stub_spawns_subagents_true_on_dispatch_verb():
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n  "
        "queue_scope: project\n  disposition: open\n  body: |\n    dispatch a reviewer\n"
    )
    result = stub_spawns_subagents(_ctx(plan_body=body))
    assert result is True


def test_stub_spawns_subagents_false_when_no_verb_present():
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n  "
        "queue_scope: project\n  disposition: open\n  body: |\n    write the file\n"
    )
    result = stub_spawns_subagents(_ctx(plan_body=body))
    assert result is False


# ---------------------------------------------------------------------------
# :153 concurrency_shared_state


def test_concurrency_shared_state_no_plan_is_undetermined():
    result = concurrency_shared_state(_ctx(plan_body=None))
    assert result["undetermined"] is True


def test_concurrency_shared_state_matches_state_path():
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: state/sizings/x.yaml\n  "
        "queue_scope: project\n  disposition: open\n  body: |\n    x\n"
    )
    result = concurrency_shared_state(_ctx(plan_body=body))
    assert result == {"candidate": True, "matched_paths": ["state/sizings/x.yaml"]}


def test_concurrency_shared_state_no_match():
    body = _spine(
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: coordinator_core/foo.py\n  "
        "queue_scope: project\n  disposition: open\n  body: |\n    x\n"
    )
    result = concurrency_shared_state(_ctx(plan_body=body))
    assert result == {"candidate": False, "matched_paths": []}


# ---------------------------------------------------------------------------
# :172 chunk_index_sidecar


def test_chunk_index_sidecar_no_plan_is_undetermined():
    result = chunk_index_sidecar(_ctx(plan_body=None))
    assert result["undetermined"] is True


def test_chunk_index_sidecar_unnamed_is_undetermined():
    result = chunk_index_sidecar(_ctx(plan_body="## Tasks\n\nno mention of any sidecar.\n"))
    assert result["undetermined"] is True


def test_chunk_index_sidecar_named_and_present(tmp_path: Path):
    sidecar = tmp_path / "state" / "chunk-index.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("{}", encoding="utf-8")
    body = "See the chunk-index sidecar at `state/chunk-index.json`.\n"
    result = chunk_index_sidecar(_ctx(plan_body=body, repo_root=tmp_path))
    assert result == {"exists": True, "path": "state/chunk-index.json"}


def test_chunk_index_sidecar_named_and_absent(tmp_path: Path):
    body = "See the chunk-index sidecar at `state/chunk-index.json`.\n"
    result = chunk_index_sidecar(_ctx(plan_body=body, repo_root=tmp_path))
    assert result == {"exists": False, "path": "state/chunk-index.json"}
