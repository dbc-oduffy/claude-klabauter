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


def test_version_consistency_directive_carries_resolved_repo_root(monkeypatch) -> None:
    monkeypatch.setattr(
        wwc_brief, "_resolve_repo_root_for_doc_staleness", lambda: "/resolved/repo/root"
    )
    directives = {d["id"]: d for d in wwc_brief._build_directives()}
    entry = directives["d_step4b_4k_version_consistency"]
    assert entry["cli"] == "check-version-consistency"
    assert entry["args"] == ["--repo-root", "/resolved/repo/root"]


def test_version_consistency_directive_omits_repo_root_when_unresolved(monkeypatch) -> None:
    monkeypatch.setattr(wwc_brief, "_resolve_repo_root_for_doc_staleness", lambda: None)
    directives = {d["id"]: d for d in wwc_brief._build_directives()}
    entry = directives["d_step4b_4k_version_consistency"]
    assert entry["args"] == []


def test_cruft_sweep_and_initiative_candidates_share_the_same_resolved_root(monkeypatch) -> None:
    calls = {"n": 0}

    def _once() -> str:
        calls["n"] += 1
        return "/resolved/repo/root"

    monkeypatch.setattr(wwc_brief, "_resolve_repo_root_for_doc_staleness", _once)
    directives = {d["id"]: d for d in wwc_brief._build_directives()}
    assert calls["n"] == 1, (
        f"_resolve_repo_root_for_doc_staleness called {calls['n']} times in one "
        "_build_directives() call, expected exactly 1 (collapsed, not re-derived per directive)"
    )
    assert directives["d_step4_counts_cruft_sweep"]["args"] == [
        "--repo-root",
        "/resolved/repo/root",
    ]
    assert directives["d_step4_counts_initiative_candidates"]["args"] == [
        "--no-stdin",
        "--root",
        "/resolved/repo/root",
    ]
    assert directives["d_step4b_4k_version_consistency"]["args"] == [
        "--repo-root",
        "/resolved/repo/root",
    ]


def test_step2_directive_names_the_validate_gate_cli_fast_subcommand() -> None:
    directive = next(
        d for d in wwc_brief._build_directives() if d["id"] == "d_step2_resolve_validation_cmd"
    )
    assert directive["cli"] == "validate-fast-and-packageability"
    assert directive["args"] == ["fast"]


def test_reap_claims_for_repos_directive_present() -> None:
    clis = {d["cli"] for d in wwc_brief._build_directives()}
    assert "reap-claims-for-repos" in clis, (
        "reap-claims-for-repos directive missing from workweek-complete: "
        "sub-reap (iii), the orphaned-claim-dir cull, has no other "
        "production caller -- deleting this directive silently disables it"
    )


def test_drift_guards_bundle_split_carries_correct_per_directive_hard_block() -> None:
    directives = {d["id"]: d for d in wwc_brief._build_directives()}
    expected = {
        "d_step4b_4k_description_length": ("description-length", False),
        "d_step4b_4k_enabled_plugins": ("enabled-plugins", False),
        "d_step4b_4k_cve_recheck": ("cve-recheck", False),
    }
    for directive_id, (subcommand, hard_block) in expected.items():
        directive = directives[directive_id]
        assert directive["cli"] == "workweek-complete-drift-guards"
        assert directive["args"] == [subcommand]
        assert directive["hard_block"] is hard_block


def test_no_directive_emits_schema_drift_gate() -> None:
    directives = wwc_brief._build_directives()
    assert "d_step4b_4k_schema_drift" not in {d["id"] for d in directives}
    for directive in directives:
        assert "schema-drift-gate" not in directive["args"], (
            f"directive {directive['id']!r} still names schema-drift-gate: "
            f"{directive['args']!r}"
        )


def test_no_directive_bundles_drift_guards_with_empty_args() -> None:
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
