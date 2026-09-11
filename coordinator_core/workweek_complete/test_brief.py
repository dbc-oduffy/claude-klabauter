"""coordinator_core/workweek_complete/test_brief.py — C4a pin: Step 2's
directive resolves through the validate gate CLI, not the standalone
resolver.

Spec backlink: docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md
(C4, Design decision 2 / Problem 3). The repoint itself and CONSUMES_MANIFEST
membership were already landed by a prior session on this branch (verified on
disk: `d_step2_resolve_validation_cmd` already carries
`cli="validate-fast-and-packageability"`, `args=["fast"]`, and
`coordinator-resolve-validation-cmd` is not, and has never needed to be, a
CONSUMES_MANIFEST member -- no other directive ever named it). This file adds
the row's own declared test surface (`coordinator_core/workweek_complete/
test_brief.py`) as a focused, file-local pin, alongside the pre-existing
broader pin in `test_workweek_complete_contract.py::
test_step2_directive_names_the_validate_gate_cli_fast_subcommand`.
"""

from __future__ import annotations

from coordinator_core.workweek_complete import brief as wwc_brief


def test_step2_directive_repointed_at_validate_gate_fast_subcommand() -> None:
    directive = next(
        d
        for d in wwc_brief._build_directives()
        if d["id"] == "d_step2_resolve_validation_cmd"
    )
    assert directive["cli"] == "validate-fast-and-packageability"
    assert directive["args"] == ["fast"]


def test_standalone_resolver_cli_is_not_a_manifest_member() -> None:
    """After the repoint, no directive names the standalone resolver CLI --
    it has no in-repo gate consumer left (AC11's note)."""
    assert "coordinator-resolve-validation-cmd" not in wwc_brief.CONSUMES_MANIFEST


def test_validate_gate_cli_is_a_manifest_member() -> None:
    assert "validate-fast-and-packageability" in wwc_brief.CONSUMES_MANIFEST
