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

import os

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


def test_step4c_ubt_directive_exists_and_hard_blocks() -> None:
    """The Step 4c UBT pending-record merge gate must exist and emit
    `hard_block: true` -- DoE's PM cut the compensating ceremony prose on the
    premise this directive exists and is trustworthy; a silently-missing or
    silently-advisory 4c directive leaves the merge gate dark on a live
    release ceremony (see brief.py's `_build_directives` docstring)."""
    directive = next(
        d for d in wwc_brief._build_directives()
        if d["id"] == "d_step4c_ubt_pending_merge_gate"
    )
    assert directive["cli"] == "workweek-complete-advisories"
    assert directive["args"][0] == "ubt-unresolved"
    assert len(directive["args"]) == 2, (
        "ubt-unresolved takes exactly one positional repo-root argument"
    )
    assert directive["hard_block"] is True


def test_only_scan_unresolved_ubt_records_caller_is_the_4c_directive() -> None:
    """Ground-truth pin (prior investigation, dispatch brief): the ONLY
    directive naming `ubt-unresolved` must be the 4c gate -- catches a
    future accidental duplicate/second invocation."""
    ubt_directives = [
        d for d in wwc_brief._build_directives()
        if d["cli"] == "workweek-complete-advisories" and d["args"] and d["args"][0] == "ubt-unresolved"
    ]
    assert [d["id"] for d in ubt_directives] == ["d_step4c_ubt_pending_merge_gate"]


def test_drift_guards_bundle_split_carries_correct_per_directive_hard_block() -> None:
    """`workweek-complete-drift-guards` bundles subcommands of differing
    severity (Task 2) -- each split-out directive must carry the severity
    correct for ITS OWN subcommand, not a single shared boolean."""
    directives = {d["id"]: d for d in wwc_brief._build_directives()}
    expected = {
        "d_step4b_4k_description_length": ("description-length", False),
        "d_step4b_4k_enabled_plugins": ("enabled-plugins", False),
        "d_step4b_4k_cve_recheck": ("cve-recheck", False),
        "d_step4b_4k_schema_drift": ("schema-drift-gate", True),
    }
    for directive_id, (subcommand, hard_block) in expected.items():
        directive = directives[directive_id]
        assert directive["cli"] == "workweek-complete-drift-guards"
        assert directive["args"] == [subcommand]
        assert directive["hard_block"] is hard_block


def test_no_directive_bundles_drift_guards_with_empty_args() -> None:
    """The pre-split single `d_step4b_4k_drift_guards` directive named the
    bundling CLI with `args=[]` -- an argparse-required-subcommand CLI
    cannot dispatch on that shape. No directive should reintroduce it.

    Introspects `workweek-complete-drift-guards.py`'s real `argparse` config
    (`build_parser()`) rather than hand-listing its subcommand names --
    <!-- Review: coordinatorcode-reviewer-fa856c15 -- prior version only
    asserted `d["args"]` truthiness against a hand-maintained expectation,
    which would not catch the next bundled-subcommand CLI shipped with
    `args=[]` against a `required=True` subparser. This walks the CLI's own
    parser so a future regression of this shape fails here too. -->
    a directive naming a bundling CLI with an unrecognized or empty leading
    arg fails against the CLI's own declared subcommand set, not a copy of
    it."""
    import importlib.util

    bin_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "coordinator", "bin",
    )
    script = os.path.join(bin_dir, "workweek-complete-drift-guards.py")
    spec = importlib.util.spec_from_file_location(
        "workweek_complete_drift_guards_cli", script
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    parser = module.build_parser()
    subparsers_action = next(
        action for action in parser._subparsers._group_actions
        if action.dest == "subcommand"
    )
    real_subcommands = set(subparsers_action.choices.keys())
    assert subparsers_action.required is True, (
        "workweek-complete-drift-guards.py's subcommand subparser is no "
        "longer required=True -- this test's premise (args=[] cannot "
        "dispatch) needs re-checking"
    )

    for d in wwc_brief._build_directives():
        if d["cli"] == "workweek-complete-drift-guards":
            assert d["args"], (
                f"directive {d['id']!r} names workweek-complete-drift-guards "
                "with no subcommand in args"
            )
            assert d["args"][0] in real_subcommands, (
                f"directive {d['id']!r} names subcommand {d['args'][0]!r}, "
                f"not one of the CLI's real declared subcommands "
                f"{sorted(real_subcommands)!r}"
            )


def test_pcli_drift_gate_is_never_a_directive() -> None:
    """The ceremony doc's own prose: pcli-04 drift gate runs by hand at
    Step 5, no directive emits its subcommand."""
    for d in wwc_brief._build_directives():
        assert d["args"][:1] != ["pcli-drift-gate"], (
            f"directive {d['id']!r} emits pcli-drift-gate, which must stay a "
            "by-hand Step 5 invocation per the ceremony doc"
        )


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
