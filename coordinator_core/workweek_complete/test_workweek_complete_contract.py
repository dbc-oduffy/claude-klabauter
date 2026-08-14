"""
coordinator_core.workweek_complete.test_workweek_complete_contract — per-field
CONSUMES_MANIFEST <-> emitted-directives conformance test for the
`workweek-complete` computed-skill engine (AC10, docs/plans/2026-07-24-b1-
ceremony-complete-computed-conversion.md).

Purpose: mirrors `workday_complete.test_workday_complete_contract` (C2's
counterpart) for the workweek assembler -- asserts CONSUMES_MANIFEST and the
actually-emitted `directives[].cli` values stay in lockstep, one assertion
PER manifest row rather than one aggregate set-equality check, so a single
dropped or orphaned CLI name fails on its own line.

Run scoped only:
    python3 -m pytest coordinator_core/workweek_complete/test_workweek_complete_contract.py -q
Spec backlink: DoE-claude:pln-b1-ceremony-complete-computed--9ffa54 § AC10
"""

from __future__ import annotations

import pytest

from coordinator_core.workweek_complete import brief as wwc_brief


def _all_emitted_directive_clis() -> set[str]:
    """workweek's `_build_directives` takes no arguments (no conditional
    branch, unlike workday's C4 day-goal directive) -- one call surfaces
    the full emission set."""
    return {d["cli"] for d in wwc_brief._build_directives()}


def test_manifest_has_no_duplicate_entries() -> None:
    manifest = wwc_brief.CONSUMES_MANIFEST
    assert len(manifest) == len(set(manifest)), (
        f"CONSUMES_MANIFEST carries a duplicate entry: {manifest!r}"
    )


def test_every_emitted_directive_cli_is_a_manifest_member() -> None:
    """No directive names a CLI CONSUMES_MANIFEST doesn't enumerate."""
    manifest = set(wwc_brief.CONSUMES_MANIFEST)
    for cli in sorted(_all_emitted_directive_clis()):
        assert cli in manifest, (
            f"directive emits CLI {cli!r}, which is absent from CONSUMES_MANIFEST "
            "-- a phantom verb reaching the apply half unlisted (AC15c)"
        )


def test_every_manifest_entry_is_named_by_at_least_one_directive() -> None:
    """No CONSUMES_MANIFEST row is a dead census entry no directive
    exercises. One assertion per manifest field (AC10's own text)."""
    emitted = _all_emitted_directive_clis()
    for cli in wwc_brief.CONSUMES_MANIFEST:
        assert cli in emitted, (
            f"CONSUMES_MANIFEST names {cli!r}, but no directive ever emits it "
            "-- either a dead census row or a directive silently dropped"
        )


def test_step2_directive_names_the_validate_gate_cli_fast_subcommand() -> None:
    """`d_step2_resolve_validation_cmd` must be repointed at the validate
    gate CLI's `fast` subcommand (`validate-fast-and-packageability`,
    `args=["fast"]`), not the standalone `coordinator-resolve-validation-cmd`
    -- pinned literally so a future silent repoint fails loudly (C4a,
    docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md § Design
    decision 2 / Problem 3: this is the fix that puts gate 3 on the
    diff-scoping seam alongside gates 1/2, with zero new CLI surface)."""
    directive = next(
        d for d in wwc_brief._build_directives() if d["id"] == "d_step2_resolve_validation_cmd"
    )
    assert directive["cli"] == "validate-fast-and-packageability"
    assert directive["args"] == ["fast"]


@pytest.mark.real_home
def test_brief_envelope_preflight_consumes_manifest_matches_module_constant() -> None:
    """The 8-key envelope's `preflight.consumes_manifest` field must be
    byte-identical to the module's own `CONSUMES_MANIFEST` constant --
    catches a `brief()` author hand-copying a stale list into the envelope
    instead of deriving it from the one source of truth.

    `real_home`: mirrors `workday_complete`'s counterpart test -- `brief()`
    resolves the real machine-local registry, which the suite-root HOME
    quarantine (`conftest.py`) would otherwise blank out, tripping the
    never-fail-the-ceremony backstop instead of exercising the real path."""
    _, envelope = wwc_brief.brief()
    assert envelope["preflight"]["consumes_manifest"] == list(
        wwc_brief.CONSUMES_MANIFEST
    )
