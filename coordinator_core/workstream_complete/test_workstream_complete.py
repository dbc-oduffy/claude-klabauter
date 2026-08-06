"""
coordinator_core.workstream_complete.test_workstream_complete — conformance
suite for the `workstream-complete-assemble` computed-skill engine.

Scope (per dispatch brief, chunk W2-B1): the assembler emits a schema-valid
8-key envelope through `emit()`; `directives[]` name real, on-disk CLIs
(never invoked in-process); `judgment_points[]` are built via the shared
constructors, and the untrusted-gate ones carry no `recommendation`.

Run scoped only: `python -m pytest coordinator_core/workstream_complete/test_workstream_complete.py -q`
Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md, chunk W2-B1
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ceremony_common import apply_halt
from coordinator_core.contract.decision_object.envelope import ENVELOPE_KEYS
from coordinator_core.testing.doe_root import resolve_doe_root
import coordinator_core.workstream_complete as wsc
from coordinator_core.workstream_complete import apply as wsc_apply
from coordinator_core.workstream_complete import chain_partition_verdict_store
from coordinator_core.workstream_complete import judgments


def _load_session_disposition_module():
    """Loads the REAL `coordinator/bin/wsc-session-disposition.py` producer
    by file path — same idiom as `coordinator/bin/tests/test_wsc_session_
    disposition.py`'s own `_load_cli_module` (that file's hyphenated
    filename bars a plain `import`). Used only by the AC4/AC8/AC9 tests
    below that must derive a `SessionShapeGate.detection` record from the
    real `_resolve_crash_recovery`/`_detection` producer functions rather
    than a hand-authored dict — state/lessons/0000-00-00-green-tests-can-
    encode-the-bug-verify-producer-consumer-key.yaml is exactly the failure
    mode this loader exists to avoid re-introducing."""
    bin_dir = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
    loader = importlib.machinery.SourceFileLoader(
        "wsc_session_disposition_c4", str(bin_dir / "wsc-session-disposition.py")
    )
    spec = importlib.util.spec_from_loader("wsc_session_disposition_c4", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_session_disposition = _load_session_disposition_module()


def _gate(
    disposition: str,
    consumed_handoff: str = "",
    diagnostics: list[str] | None = None,
    consumed_handoff_paths: tuple[str, ...] | None = None,
    detection: dict[str, object] | None = None,
) -> wsc.SessionShapeGate:
    # C3B widened SessionShapeGate with a plural `consumed_handoff_paths`
    # field (`primary_consumed_handoff`'s own full sorted `matches` list).
    # Default it from the scalar `consumed_handoff` when the caller doesn't
    # supply the plural form explicitly, mirroring `resolve_disposition`'s
    # own "scalar == plural[0], or empty" contract.
    if consumed_handoff_paths is None:
        consumed_handoff_paths = (consumed_handoff,) if consumed_handoff else ()
    return wsc.SessionShapeGate(
        sid="testsid123",
        disposition=disposition,
        consumed_handoff=consumed_handoff,
        diagnostics=diagnostics or [],
        consumed_handoff_paths=consumed_handoff_paths,
        # `detection` is `wsc-session-disposition.py`'s structured record and
        # is the ONLY input `_session_shape_is_uncertain` reads. `diagnostics`
        # above is prose for a human and gates nothing — a fixture that sets
        # one must never be assumed to have set the other.
        detection=detection or {},
    )


def _patch_gate(monkeypatch: pytest.MonkeyPatch, gate: wsc.SessionShapeGate) -> None:
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: gate)


# ---------------------------------------------------------------------------
# Envelope conformance
# ---------------------------------------------------------------------------


def test_brief_emits_exactly_the_8_canonical_keys(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    assert set(decision_object.keys()) == set(ENVELOPE_KEYS)


def test_brief_decisions_key_echoes_caller_supplied_decisions(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {"governing_plan_slug": "some-plan"}
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    assert decision_object["decisions"] == decisions


def test_brief_raises_transport_failure_when_repo_root_unresolvable(monkeypatch):
    monkeypatch.setattr(wsc, "resolve_repo_root", lambda start=None: None)
    with pytest.raises(wsc.TransportFailure):
        wsc.brief(decisions={})


# ---------------------------------------------------------------------------
# directives[] — every named cli is a real on-disk script, never invoked
# ---------------------------------------------------------------------------

#: The C2a-C2i multi-module expansion (`docs/plans/2026-07-26-workstream-
#: complete-computed-frontage.md`) grew this from a closed 4-CLI set to
#: `CONSUMES_MANIFEST`'s full ~20-entry surface -- derived from the module's
#: own constant rather than re-hardcoded here as a second literal, so the
#: two cannot drift apart the way this set (authored for Convert #2, before
#: the expansion) just did against the live assembler.
_KNOWN_CLIS = set(wsc.CONSUMES_MANIFEST)


def test_directives_only_name_known_real_clis_and_never_invoke_them(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decisions = {
        "governing_plan_slug": "plan-slug",
        "subject": "a commit subject",
        "review": {
            "sha_range": "a..b",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        },
        "msg_file": str(tmp_path / "msg.txt"),
    }
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    directives = decision_object["directives"]
    assert directives, "expected at least one directive on the chain-terminal path"
    for directive in directives:
        assert set(directive.keys()) == {"id", "cli", "args", "depends_on", "already_satisfied"}
        assert directive["cli"] in _KNOWN_CLIS
        # A CONSUMES_MANIFEST member ships as either `<name>.py` or a
        # bareword launcher shim (e.g. `emit-cadence`, `session-claim-cli`,
        # `coordinator-fold-execution-record`) -- a raw `_BIN_DIR /
        # directive["cli"]` join only matches the `.py`-suffixed shape and
        # false-fails every bareword member the C2a-C2i expansion legitimately
        # added. Reuse apply.py's own `_resolve_script_path` (the module
        # that already owns this exact two-candidate resolution for its
        # closed dispatch table) rather than re-deriving the convention here.
        resolved = wsc_apply._resolve_script_path(directive["cli"])  # noqa: SLF001 - shared resolution helper, not reimplemented
        assert resolved.is_file(), f"{directive['cli']} does not exist on disk (resolved to {resolved})"


def test_every_consumes_manifest_member_resolves_to_a_real_file_on_disk() -> None:
    """Full-manifest disk-existence guard — the seam the prior test's
    per-sweep membership check does NOT close.

    `test_directives_only_name_known_real_clis_and_never_invoke_them` above
    only inspects `directives[]` actually EMITTED under one specific
    `decisions` payload (the chain-terminal sweep) — a `CONSUMES_MANIFEST`
    member whose directive never fires under THAT sweep (e.g. because it
    needs caller-supplied `decisions["lessons"]`, a real governing-plan
    file, or another fact the sweep doesn't set — see `__init__.py`'s own
    "Coverage caveat" Negative-spec paragraph) is invisible to it.
    `test_workstream_complete_contract.py`'s own guard is membership-only
    (every directive's `cli` is IN the manifest) and never touches disk.
    Neither guard, alone or together, asserts that EVERY manifest member
    resolves to a real `coordinator/bin/` script — a phantom entry sits
    exactly in that gap (2026-07-27 finding: `scan_unresolved_ubt_records.py`
    had no on-disk CLI despite being a `CONSUMES_MANIFEST` member and a
    live directive's `cli`, so `d-run-ubt-pending-check` could fire and
    silently fail with `FileNotFoundError` on the one occasion its gate
    ever opened). This test closes the gap directly: walk the manifest
    itself, not any one sweep's emitted subset, and require every member to
    resolve to a real file. No member is exempted — a manifest entry that
    cannot be dispatched is a directive that was never truly ready to ship,
    not a legitimate "dispatched-worker-only" carve-out.
    """
    for cli_name in wsc.CONSUMES_MANIFEST:
        resolved = wsc_apply._resolve_script_path(cli_name)  # noqa: SLF001 - shared resolution helper, not reimplemented
        assert resolved.is_file(), (
            f"CONSUMES_MANIFEST member {cli_name!r} does not resolve to a real "
            f"file on disk (resolved to {resolved}) — any directive naming this "
            "cli would raise FileNotFoundError at dispatch time the one time its "
            "gate opens"
        )


def test_chain_terminal_with_consumed_handoff_computes_coverage_gate_directive(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-coverage-gate" in ids
    # Convert #2's original `d-tail` id is SUPERSEDED by C2e's
    # `directives_commit_tail.build_wsc_tail_directive` (id renamed to
    # `d-run-wsc-tail` -- the census's own name for the Step 3 keystone
    # call). See __init__.py's module-docstring Negative-spec.
    assert "d-run-wsc-tail" in ids


#: Every directive id that constitutes a brightline-class gate, in EITHER
#: scope. The pin below asserts membership in this set rather than one
#: specific id, so a future re-scoping of the chain gate keeps the invariant
#: (SOME brightline gate fires) without needing this test rewritten — the
#: only edit that may legitimately shrink it is one that deletes a gate.
_BRIGHTLINE_DIRECTIVE_IDS = frozenset(
    {"d-run-review-brightline-gate", "d-run-chain-plan-brightline-gate"}
)


def test_every_disposition_computes_some_brightline_gate_directive(monkeypatch, tmp_path):
    """The invariant the 2026-08-03 example-doctrine-repo-em memo found violated: a
    chain-terminal close skipped the session-scoped brightline gate (right
    scope call) and substituted nothing, leaving the close that caps an
    entire lineage's diff as the ONLY one with no brightline gate at all.

    `d-coverage-gate` does not discharge this: `jp-coverage-verdict` is
    advisory by design and cannot block a complete, so a test asserting only
    the coverage gate (as
    `test_chain_terminal_with_consumed_handoff_computes_coverage_gate_directive`
    does) stays green through exactly this hole. Asserted per disposition,
    not just for the chain terminal, so the mid-chain leg cannot regress
    into the same silence.
    """
    for disposition, consumed_handoff in (
        ("single-session", ""),
        ("chain-terminal", "state/handoffs/x.md"),
        ("chain-terminal", ""),  # no resolvable closing handoff — still gated, via the session gate
    ):
        _patch_gate(monkeypatch, _gate(disposition, consumed_handoff=consumed_handoff))
        decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
        ids = {d["id"] for d in decision_object["directives"]}
        assert ids & _BRIGHTLINE_DIRECTIVE_IDS, (
            f"disposition {disposition!r} computed NO brightline-class directive "
            f"(got {sorted(ids)}) — the highest-risk close must not be less gated "
            "than an ordinary session"
        )


def test_chain_terminal_brightline_gate_is_chain_scoped_not_session_scoped(monkeypatch, tmp_path):
    """The session-scoped gate is the WRONG scope for a chain terminal — the
    substitution must be the chain+plan two-oracle gate over the closing
    handoff, not the session gate re-emitted."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md"))
    directives = wsc.brief(decisions={}, repo_root=tmp_path)["directives"]
    ids = {d["id"] for d in directives}
    assert "d-run-chain-plan-brightline-gate" in ids
    assert "d-run-review-brightline-gate" not in ids
    gate = next(d for d in directives if d["id"] == "d-run-chain-plan-brightline-gate")
    assert gate["cli"] == "wsc-coverage-gate-runner"
    assert gate["args"] == ["brightline-gate", "--from-handoff", "state/handoffs/x.md"]


def test_chain_terminal_without_consumed_handoff_falls_back_to_session_gate(monkeypatch, tmp_path):
    """`--from-handoff` is required by the runner's own parser, so a chain
    terminal with no resolved closing handoff cannot take the chain gate —
    but the fallback is the session-scoped gate, NOT silence.

    Emitting nothing here would reinstate the memo's own violated invariant
    on a narrower path: a chain terminal with strictly less brightline
    gating than an ordinary session. The session gate is always
    constructible (it needs only `sid`), and a narrower-scoped gate is
    strictly more than none — wrong-scope-but-present beats absent.
    """
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=""))
    ids = {d["id"] for d in wsc.brief(decisions={}, repo_root=tmp_path)["directives"]}
    assert "d-run-review-brightline-gate" in ids
    assert "d-run-chain-plan-brightline-gate" not in ids


def test_single_session_computes_no_coverage_gate_directive(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-coverage-gate" not in ids
    # Same rename as above -- `d-tail` -> `d-run-wsc-tail`.
    assert "d-run-wsc-tail" in ids


def test_write_trail_directive_requires_all_five_review_fields(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    partial_decisions = {"review": {"sha_range": "a..b", "reviewer": "someone"}}
    decision_object = wsc.brief(decisions=partial_decisions, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-write-trail" not in ids


def test_deletion_blocks_directive_absent_when_no_msg_file(monkeypatch, tmp_path):
    """2026-07-27 finding: `d-deletion-blocks` names a CLI whose one
    positional (`<prepared-commit-msg-file>`) is REQUIRED — a `msg_file`-
    less session must emit no directive at all, never one with an empty
    `args` list that would fail with a usage error (exit 2) on every real
    `apply` run. Mirrors `d-release-plan-claim`'s "absent input, no
    directive" convention (see `test_no_release_plan_directive_when_no_
    governing_plan_resolved` below for that sibling case)."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-deletion-blocks" not in ids


def test_deletion_blocks_directive_present_with_msg_file_path(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    msg_file = str(tmp_path / "msg.txt")
    decision_object = wsc.brief(decisions={"msg_file": msg_file}, repo_root=tmp_path)
    directives_by_id = {d["id"]: d for d in decision_object["directives"]}
    assert "d-deletion-blocks" in directives_by_id
    assert directives_by_id["d-deletion-blocks"]["args"] == [msg_file]


def test_build_deletion_blocks_check_directive_returns_none_for_falsy_msg_file():
    assert wsc.build_deletion_blocks_check_directive(None) is None
    assert wsc.build_deletion_blocks_check_directive("") is None


# ---------------------------------------------------------------------------
# jp-coverage-verdict -- ADVISORY, not enforced (examined and confirmed
# as-designed; see `state/lessons/2026-07-27-verify-a-gate-actually-
# enforces-before-s-a20579f1aa06.yaml`). This section only pins the phantom-
# id fix (2026-07-27): the disposition previously named a "d-tail" id no
# directive ever emits. It is removed, never replaced with a real directive
# id, so this judgment point stays advisory -- picking any disposition,
# including the halt one, resolves nothing that gates the commit tail.
# ---------------------------------------------------------------------------


def test_coverage_judgment_point_resolves_no_phantom_or_enforcing_id(monkeypatch, tmp_path):
    """`covered`/`uncovered-or-indeterminate-override` resolve only the
    real, decisions-gated `d-write-trail` directive -- never the removed
    phantom `d-tail`, and never `d-run-wsc-tail` (that would silently
    re-introduce enforcement this judgment point does not have). The
    `uncovered-or-indeterminate-proceed-with-warning` disposition (renamed
    from `-halt`, which promised an enforcement this judgment point never
    performed) resolves nothing at all, by design."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    matches = [jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-coverage-verdict"]
    assert len(matches) == 1
    dispositions_by_value = {d["value"]: d["resolves"] for d in matches[0]["dispositions"]}
    assert dispositions_by_value["covered"] == ["d-write-trail"]
    assert dispositions_by_value["uncovered-or-indeterminate-override"] == ["d-write-trail"]
    assert dispositions_by_value["uncovered-or-indeterminate-proceed-with-warning"] == []
    assert "uncovered-or-indeterminate-halt" not in dispositions_by_value


def test_commit_tail_directive_carries_no_dependency_on_the_coverage_judgment(monkeypatch, tmp_path):
    """The commit tail (`d-run-wsc-tail`) must NOT gain a dependency edge on
    `jp-coverage-verdict` or on `d-coverage-gate` -- the gate is advisory,
    and wiring an edge here would be an enforcement change the PM declined
    (2026-07-27). `depends_on` must always carry the pre-existing ordering
    member (`d-close-tail-args`), on both the chain-terminal (coverage-gate-
    present) and single-session (no-coverage-gate) legs -- this pins the
    absence of the coverage edge specifically, so a future session doesn't
    silently re-land it. It does NOT pin `depends_on` to that single member
    exactly -- `jp-completion-entry-scaffold`/`jp-commit-subject-missing`
    (state/bug-backlog/2026-07-28-workstream-complete-apply-re-scaffolds-t-
    e925d597e0af.yaml) legitimately add further, UNRELATED dependency
    members onto the same directive under `decisions={}` (no subject
    supplied); asserting exact list identity here would break on every
    such legitimate addition without testing this test's own actual
    invariant."""
    for gate in (
        _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()),
        _gate("single-session", consumed_handoff_paths=()),
    ):
        _patch_gate(monkeypatch, gate)
        decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
        wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
        depends_on = wsc_tail["depends_on"]
        depends_on_list = [depends_on] if isinstance(depends_on, str) else list(depends_on)
        assert "d-close-tail-args" in depends_on_list
        assert "jp-coverage-verdict" not in depends_on_list
        assert "d-coverage-gate" not in depends_on_list


# ---------------------------------------------------------------------------
# decide_review_scale wiring (2026-08-03-chain-end-review-scale-wiring.md,
# chunk C4) -- AC2's deliverable and the regression pin for the whole defect
# class the source memo found: decide_review_scale had zero call sites, so
# rows 5/6 (the chain-terminal rows) were unreachable no matter what a
# caller passed. Every test below asserts through `wsc.brief()`, never by
# calling `decide_review_scale` directly -- a unit test on the pure function
# is exactly the test shape (C1's own) that stayed green through this
# defect's entire life; it proves nothing about whether anything CALLS it.
# ---------------------------------------------------------------------------

_CHAIN_END_ROW_IDS = frozenset({5, 6})


def _review_scale_decisions(**overrides: Any) -> dict:
    base: dict[str, Any] = dict(
        gross_loc=10,
        code_loc=10,
        commit_count=1,
        surface_count=1,
        executor_dispatched=False,
        shared_schema_touched=False,
        chain_partition_verdict="single-reviewer-ok",
    )
    base.update(overrides)
    return base


def test_chain_terminal_non_trivial_chain_diff_below_brightline_selects_row_5(monkeypatch, tmp_path):
    """(a) chain-terminal + a resolved, non-mandatory chain-scoped verdict
    -> row 5 (code-reviewer), reachable end-to-end through brief()."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions=_review_scale_decisions(), repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is True
    assert review_scale["row"] in _CHAIN_END_ROW_IDS
    assert review_scale["row"] == 5
    assert review_scale["scale"] == "code-reviewer"
    assert review_scale["partition_mandatory"] is False


def test_chain_terminal_over_the_brightline_selects_row_6_partition_mandatory(monkeypatch, tmp_path):
    """(b) chain-terminal + the chain-scoped brightline gate's own verdict
    is PARTITION-MANDATORY -> row 6, partition_mandatory True."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(
        decisions=_review_scale_decisions(chain_partition_verdict="PARTITION-MANDATORY"),
        repo_root=tmp_path,
    )
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is True
    assert review_scale["row"] in _CHAIN_END_ROW_IDS
    assert review_scale["row"] == 6
    assert review_scale["scale"] == "partitioned"
    assert review_scale["partition_mandatory"] is True


def test_chain_terminal_unresolved_triviality_never_falls_through_to_a_per_session_row(monkeypatch, tmp_path):
    """(c) chain-terminal with `chain_partition_verdict` not yet supplied ->
    the unresolved outcome C1 defines, and NEVER a silent per-session row
    (1/2/3) -- the exact shape of the original defect, under a new name."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(
        decisions=_review_scale_decisions(chain_partition_verdict=None),
        repo_root=tmp_path,
    )
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is False
    assert review_scale["row"] is None
    assert review_scale["row"] not in {1, 2, 3}
    assert review_scale["scale"] == "unresolved"
    assert review_scale["partition_mandatory"] is False


def test_review_scale_judgment_point_is_advisory_no_dependency_edge_on_commit_tail(monkeypatch, tmp_path):
    """(d) the ADVISORY posture (C0's implemented default) actually holds:
    `d-run-wsc-tail` carries NO dependency edge on `jp-review-scale`, on
    either a chain-terminal or a single-session close, so a future reader
    cannot mistake the edge's absence for an oversight."""
    for gate, decisions in (
        (
            _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()),
            _review_scale_decisions(chain_partition_verdict="PARTITION-MANDATORY"),
        ),
        (_gate("single-session", consumed_handoff_paths=()), {}),
    ):
        _patch_gate(monkeypatch, gate)
        decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
        ids = {jp["id"] for jp in decision_object["judgment_points"]}
        assert "jp-review-scale" in ids
        wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
        depends_on = wsc_tail["depends_on"]
        depends_on_list = [depends_on] if isinstance(depends_on, str) else list(depends_on)
        assert "d-close-tail-args" in depends_on_list
        assert "jp-review-scale" not in depends_on_list


def test_review_scale_judgment_point_does_not_fire_on_a_fully_resolved_row_1_or_2_close(monkeypatch, tmp_path):
    """(d2) review-integrator finding 2 / EM ruling 2026-08-03: rows 1 and 2
    mean "no review needed" -- a fully resolved single-session close landing
    on either row must NOT carry `jp-review-scale` at all, unlike the
    unresolved / row 5/6 cases above which still fire. This is the
    complement of test_review_scale_judgment_point_is_advisory_no_dependency_
    edge_on_commit_tail's "still fires" assertion, not a replacement for it."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    for code_loc, expected_row in ((0, 1), (10, 2)):
        decision_object = wsc.brief(
            decisions=_review_scale_decisions(code_loc=code_loc),
            repo_root=tmp_path,
        )
        review_scale = decision_object["gates"]["review_scale"]
        assert review_scale["resolved"] is True
        assert review_scale["row"] == expected_row
        ids = {jp["id"] for jp in decision_object["judgment_points"]}
        assert "jp-review-scale" not in ids


def test_review_scale_judgment_point_unresolved_carries_no_recommendation(monkeypatch, tmp_path):
    """(e) example-retrieval-repo-em memo (cross-repo/inbox/2026-08-04-example-retrieval-repo-em-
    brightline-partition-mandatory-does-not-halt.md, "mechanism 3"): an
    unresolved chain-terminal review-scale decision must NOT come with a
    `proceed-unresolved` recommendation -- that recommendation is what let
    an EM route around a brightline gate's own PARTITION-MANDATORY verdict
    when it wasn't carried forward. `jp-review-scale` must still fire (the
    unresolved state is real and must be surfaced), just with
    `recommendation is None` (the untrusted-gate shape)."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(
        decisions=_review_scale_decisions(chain_partition_verdict=None),
        repo_root=tmp_path,
    )
    jp = next(jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-review-scale")
    assert jp["recommendation"] is None


def test_review_scale_judgment_point_resolved_non_trivial_row_keeps_acknowledge_scale(monkeypatch, tmp_path):
    """(f) the RESOLVED branch is untouched by the mechanism-3 fix: a
    resolved, non-1/2 row (here row 5) still carries the trusted
    `acknowledge-scale` recommendation via `build_judgment_point`."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions=_review_scale_decisions(), repo_root=tmp_path)
    jp = next(jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-review-scale")
    assert jp["recommendation"] == {
        "disposition": "acknowledge-scale",
        "rationale": (
            "review scale row 5 (code-reviewer): chain-terminal with a resolved, "
            "non-mandatory chain-scoped brightline verdict"
        ),
    }


def test_review_scale_resolved_false_triviality_but_missing_metric_stays_unresolved(monkeypatch, tmp_path):
    """(e) the Staff Engineer finding 3: triviality resolves False (chain-scoped verdict
    is `single-reviewer-ok`, ruling out row 6) AND a row-4 metric
    (`gross_loc`) is absent -> the unresolved outcome, never row 5. A
    resolved-false chain verdict must not be read as "safe to fall through
    to row 5 regardless of what else is unknown"."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(
        decisions=_review_scale_decisions(chain_partition_verdict="single-reviewer-ok", gross_loc=None),
        repo_root=tmp_path,
    )
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is False
    assert review_scale["row"] is None
    assert review_scale["row"] != 5


# ---------------------------------------------------------------------------
# chain_partition_verdict_store fallback (2026-08-04, root-cause fix for
# cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-mandatory-
# does-not-halt.md "mechanism 2"): brief() reads the persisted verdict from
# disk when `decisions` omits `chain_partition_verdict`, but an explicit
# `decisions` value always wins, and any record that cannot be positively
# verified as belonging to this close degrades to unresolved -- never a
# fabricated verdict.
# ---------------------------------------------------------------------------

_SID = "testsid123"  # matches `_gate()`'s default `sid`
_HANDOFF = "state/handoffs/x.md"  # matches the `consumed_handoff` used below


def _persist(tmp_path: Path, *, session_id: str = _SID, verdict: str, from_handoff: str = _HANDOFF) -> None:
    chain_partition_verdict_store.write_verdict_record(
        tmp_path,
        session_id=session_id,
        verdict=verdict,
        from_handoff=from_handoff,
        git_range=None,
        basis="plan_oracle=4(...) chain_oracle=32(...) session_oracle=10(...) tier=B",
        tier="B",
    )


def test_disk_persisted_verdict_used_when_decisions_omits_it_selects_row_6(monkeypatch, tmp_path):
    """Producer wrote PARTITION-MANDATORY to disk; the EM's `decisions` dict
    never re-supplies it -- brief() must still resolve row 6, closing the
    exact defect the field report traced."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=_HANDOFF, consumed_handoff_paths=()))
    _persist(tmp_path, verdict="PARTITION-MANDATORY")
    decisions = _review_scale_decisions()
    del decisions["chain_partition_verdict"]
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is True
    assert review_scale["row"] == 6
    assert review_scale["scale"] == "partitioned"
    assert review_scale["partition_mandatory"] is True


def test_explicit_decisions_verdict_overrides_persisted_disk_record(monkeypatch, tmp_path):
    """An explicit `decisions["chain_partition_verdict"]` always wins over
    whatever is on disk -- disk is a fallback only, never an override."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=_HANDOFF, consumed_handoff_paths=()))
    _persist(tmp_path, verdict="PARTITION-MANDATORY")
    decision_object = wsc.brief(
        decisions=_review_scale_decisions(chain_partition_verdict="single-reviewer-ok"),
        repo_root=tmp_path,
    )
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["row"] == 5
    assert review_scale["partition_mandatory"] is False


def test_disk_record_from_a_different_session_is_ignored(monkeypatch, tmp_path):
    """A record keyed to a DIFFERENT session id (e.g. a stale/foreign run)
    must never be adopted -- it degrades to unresolved, exactly as if no
    record existed."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=_HANDOFF, consumed_handoff_paths=()))
    _persist(tmp_path, session_id="some-other-session", verdict="PARTITION-MANDATORY")
    decisions = _review_scale_decisions()
    del decisions["chain_partition_verdict"]
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is False
    assert review_scale["scale"] == "unresolved"


def test_disk_record_with_mismatched_from_handoff_is_ignored(monkeypatch, tmp_path):
    """A record computed over a DIFFERENT handoff (stale provenance) must
    never be adopted for this close."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=_HANDOFF, consumed_handoff_paths=()))
    _persist(tmp_path, verdict="PARTITION-MANDATORY", from_handoff="state/handoffs/some-other-run.md")
    decisions = _review_scale_decisions()
    del decisions["chain_partition_verdict"]
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is False
    assert review_scale["scale"] == "unresolved"


def test_corrupt_disk_record_degrades_to_unresolved_never_fabricates(monkeypatch, tmp_path):
    """Corrupt JSON on disk must never crash brief() nor manufacture a
    verdict -- fail-closed to unresolved."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=_HANDOFF, consumed_handoff_paths=()))
    path = chain_partition_verdict_store.verdict_store_path(tmp_path, _SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    decisions = _review_scale_decisions()
    del decisions["chain_partition_verdict"]
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is False
    assert review_scale["scale"] == "unresolved"


def test_missing_disk_record_degrades_to_unresolved(monkeypatch, tmp_path):
    """No record on disk at all (never persisted, or a fresh tmp_path) ->
    unresolved -- the pre-existing behavior, unchanged."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=_HANDOFF, consumed_handoff_paths=()))
    decisions = _review_scale_decisions()
    del decisions["chain_partition_verdict"]
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is False
    assert review_scale["scale"] == "unresolved"


def test_unknown_verdict_string_on_disk_degrades_to_unresolved(monkeypatch, tmp_path):
    """A verdict string outside the two known literals (e.g. corruption or
    a future schema drift) is treated as absent, never adopted."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=_HANDOFF, consumed_handoff_paths=()))
    path = chain_partition_verdict_store.verdict_store_path(tmp_path, _SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    path.write_text(
        _json.dumps(
            {
                "schema_version": 1,
                "session_id": _SID,
                "verdict": "totally-unknown-verdict",
                "from_handoff": _HANDOFF,
                "git_range": None,
                "basis": "",
                "tier": "B",
                "written_at": "2026-08-04T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    decisions = _review_scale_decisions()
    del decisions["chain_partition_verdict"]
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is False
    assert review_scale["scale"] == "unresolved"


def test_brief_reading_persisted_verdict_still_mutates_nothing(monkeypatch, tmp_path):
    """brief() is documented read-only -- reading the persisted verdict
    record must not write, touch, or delete it (or anything else under
    tmp_path)."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff=_HANDOFF, consumed_handoff_paths=()))
    _persist(tmp_path, verdict="PARTITION-MANDATORY")
    path = chain_partition_verdict_store.verdict_store_path(tmp_path, _SID)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    files_before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())

    decisions = _review_scale_decisions()
    del decisions["chain_partition_verdict"]
    wsc.brief(decisions=decisions, repo_root=tmp_path)

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    files_after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert files_after == files_before


# ---------------------------------------------------------------------------
# d-archive-session-claim removed from the assembly (2026-07-28) -- this
# ceremony fires once per closed workstream, but session-dir archival
# (`scope.archive()`) is a once-per-SESSION-END operation. Emitting the
# archive directive here archived a still-live session mid-session,
# destroying once-per-session sentinels and the dispatch-evidence file.
# Archival is now wired to session END (a SessionEnd hook, example-doctrine-repo repo),
# not this assembly. `d-emit-cadence` previously depended on the removed
# directive; it must still have a satisfiable dependency after the removal.
# ---------------------------------------------------------------------------


def test_directive_list_no_longer_contains_archive_session_claim(monkeypatch, tmp_path):
    for gate in (
        _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()),
        _gate("single-session", consumed_handoff_paths=()),
    ):
        _patch_gate(monkeypatch, gate)
        decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
        ids = {d["id"] for d in decision_object["directives"]}
        assert "d-archive-session-claim" not in ids


def test_emit_cadence_repointed_onto_run_wsc_tail_after_archive_removal(monkeypatch, tmp_path):
    """`d-emit-cadence` used to depend on `d-archive-session-claim`. With
    that directive gone, it must depend on `d-run-wsc-tail` instead -- the
    same directive `d-archive-session-claim` itself used to depend on -- so
    the ordering guarantee (fires only after the Step 3 commit lands) is
    preserved and the gate stays openable."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    directives_by_id = {d["id"]: d for d in decision_object["directives"]}
    assert "d-emit-cadence" in directives_by_id
    assert directives_by_id["d-emit-cadence"]["depends_on"] == "d-run-wsc-tail"
    assert directives_by_id["d-emit-cadence"]["args"] == ["{d-run-wsc-tail.landed}"], (
        "d-emit-cadence must carry the ordering-only producer-readiness token "
        "so apply() refuses to dispatch it when d-run-wsc-tail never landed -- "
        "see apply.py's _resolve_arg_tokens '.landed' field"
    )


# ---------------------------------------------------------------------------
# jp-completion-entry-scaffold / jp-commit-subject-missing (state/bug-
# backlog/2026-07-28-workstream-complete-apply-re-scaffolds-t-
# e925d597e0af.yaml) -- the two authoring-window halts in front of
# d-run-wsc-tail.
# ---------------------------------------------------------------------------


def _depends_on_list(directive: dict) -> list:
    dep = directive["depends_on"]
    return [dep] if isinstance(dep, str) else list(dep or [])


def test_completion_entry_scaffold_gate_blocks_wsc_tail_when_entry_missing(monkeypatch, tmp_path):
    (tmp_path / "archive").mkdir()
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-completion-entry-scaffold" in jp_ids
    scaffold_jp = next(jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-completion-entry-scaffold")
    # Structurally unresolvable -- its one disposition resolves nothing.
    assert scaffold_jp["dispositions"] == [{"value": "not-yet-authored", "resolves": []}]

    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-completion-entry-scaffold" in _depends_on_list(wsc_tail)


def test_completion_entry_scaffold_gate_absent_once_entry_fully_authored(monkeypatch, tmp_path):
    (tmp_path / "archive").mkdir()
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))

    from coordinator_core.workstream_complete import directives_completion

    entry_path = Path(
        directives_completion._coordinator_complete_entry.resolve_entry_path(str(tmp_path), "testsid123", "")
    )
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(
        "---\n"
        'title: "Did the thing"\n'
        "created: 2026-07-01\n"
        "nature: bugfix\n"
        "nature_inferred: false\n"
        "commits: []\n"
        "status: pending-release\n"
        "chain_terminal: false\n"
        'authored_by: "testsid123"\n'
        "loe:\n"
        "  agent_dispatches: null\n"
        "  opus_dispatches: null\n"
        "  em_tokens: null\n"
        "  tshirt: null\n"
        "---\n\nDid the thing, verified.\n",
        encoding="utf-8",
    )

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-completion-entry-scaffold" not in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-completion-entry-scaffold" not in _depends_on_list(wsc_tail)


def test_completion_entry_scaffold_gate_absent_when_chain_entry_stood_down_from_a_prior_day(monkeypatch, tmp_path):
    """chain-terminal close, multi-day chain: the real completion entry
    already exists (a prior day, a different session's sid) and is fully
    authored. `coordinator-complete-entry` itself stands down onto that
    entry rather than deriving today's date/sid path — the gate must
    consult the SAME stand-down-aware resolution, not
    `resolve_entry_path`'s date/sid derivation alone (state/bug-backlog/
    2026-07-28-workstream-complete-apply-re-scaffolds-t-e925d597e0af.yaml).
    Regression guard: both pre-existing scaffold-gate tests above only
    exercise `single-session`, where no stand-down is possible — neither
    would have caught this."""
    (tmp_path / "archive").mkdir()
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    chain_slug = "some-plan"
    prior_entry = tmp_path / "archive" / "completed" / "2026-06" / "2026-06-01-some-plan-abc123.md"
    prior_entry.parent.mkdir(parents=True, exist_ok=True)
    prior_entry.write_text(
        "---\n"
        'title: "Did the thing"\n'
        "created: 2026-06-01\n"
        "nature: bugfix\n"
        "nature_inferred: false\n"
        "commits: []\n"
        'chain: "some-plan"\n'
        "status: pending-release\n"
        "chain_terminal: true\n"
        'authored_by: "abc123"\n'
        "loe:\n"
        "  agent_dispatches: null\n"
        "  opus_dispatches: null\n"
        "  em_tokens: null\n"
        "  tshirt: null\n"
        "---\n\nDid the thing, verified.\n",
        encoding="utf-8",
    )

    decision_object = wsc.brief(
        decisions={"subject": "a commit subject", "governing_plan_slug": chain_slug}, repo_root=tmp_path
    )
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-completion-entry-scaffold" not in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-completion-entry-scaffold" not in _depends_on_list(wsc_tail)


def test_commit_subject_missing_blocks_wsc_tail_and_names_named_halt(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-commit-subject-missing" in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-commit-subject-missing" in _depends_on_list(wsc_tail)
    assert "--subject" not in wsc_tail["args"]


def test_commit_subject_resolved_from_commit_message_authoring_decision_wires_wsc_tail_args(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {
        "commit-message-authoring": {
            "disposition": "drafted",
            "subject": "fix: the thing",
            "prose": "Detailed body.",
        }
    }
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-commit-subject-missing" not in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-commit-subject-missing" not in _depends_on_list(wsc_tail)
    assert "--subject" in wsc_tail["args"]
    assert "fix: the thing" in wsc_tail["args"]
    assert "--prose" in wsc_tail["args"]
    assert "Detailed body." in wsc_tail["args"]


# ---------------------------------------------------------------------------
# jp-stage-paths-missing (state/bug-backlog/2026-07-29-workstream-complete-
# silently-under-commi-33e5cdf24112.yaml) -- the third authoring-window
# halt in front of d-run-wsc-tail. Unlike jp-commit-subject-missing (which
# guards a HARD-required wsc-tail.py flag), --stage-paths is optional at
# the CLI layer, so its omission previously failed *silent*, not loud: the
# tail committed only whatever its own dirty-tree gates independently
# swept. This gate closes that asymmetry.
# ---------------------------------------------------------------------------


def test_stage_paths_missing_blocks_wsc_tail_and_names_named_halt(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-stage-paths-missing" in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-stage-paths-missing" in _depends_on_list(wsc_tail)
    assert "--stage-paths" not in wsc_tail["args"]

    stage_paths_jp = next(jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-stage-paths-missing")
    # Structurally unresolvable -- its one disposition resolves nothing, so a
    # fabricated disposition can never clear this gate (mirrors
    # jp-commit-subject-missing's own contract).
    assert stage_paths_jp["dispositions"] == [{"value": "stage-paths-not-yet-supplied", "resolves": []}]
    assert stage_paths_jp["recommendation"] is None


def test_stage_paths_supplied_clears_the_gate_and_wires_wsc_tail_args(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {"subject": "a commit subject", "stage_paths": ["state/some-file.md", "state/other-file.md"]}
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-stage-paths-missing" not in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-stage-paths-missing" not in _depends_on_list(wsc_tail)
    assert "--stage-paths" in wsc_tail["args"]
    assert "state/some-file.md" in wsc_tail["args"]
    assert "state/other-file.md" in wsc_tail["args"]


# ---------------------------------------------------------------------------
# jp-consumed-handoff-completeness -- C4's blocking pre-commit completeness
# gate (docs/plans/2026-08-01-wsc-completeness-gate-and-pickup-successor.md).
# Leg B (`handoff.has_live_children`) is monkeypatched in every test below
# via `wsc._dispatch_has_live_children` -- the small local dispatch helper
# this chunk owns -- rather than exercised against a real op call, since
# `tmp_path` is not a real git worktree (the real dispatch path degrades to
# a leg-B `exit_code=2` indeterminate there, per `_dispatch_has_live_
# children`'s own "never raises" contract, which is exercised for real by
# the phantom-resolves-id sweep above).
# ---------------------------------------------------------------------------


def _patch_leg_b(monkeypatch: pytest.MonkeyPatch, result: dict) -> None:
    monkeypatch.setattr(wsc, "_dispatch_has_live_children", lambda root, candidate: dict(result))


def _write_ac_handoff(tmp_path: Path, rel_path: str, body: str) -> None:
    handoff_path = tmp_path / rel_path
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(f"---\nstatus: open\n---\n\n{body}\n", encoding="utf-8")


def test_leg_b_dispatch_narrows_edge_kinds_so_a_live_spinoff_does_not_block_the_close(monkeypatch, tmp_path):
    """Leg B must NOT inherit `has_live_children`'s archival default edge set.

    `forked_from` is the spinoff edge. Archival legitimately blocks on it (it
    would strand the spinoff's origin pointer); "may this workstream conclude?"
    must not, because a spinoff is forked out precisely so the parent can
    finish without it. Asserted on the params actually handed to the op —
    every other leg-B test monkeypatches `_dispatch_has_live_children` wholesale
    and so cannot see this.

    Pre-existing regression coverage for shipped production code (`_dispatch_
    has_live_children`, commit fb17badb3), outside this slice's two named
    changes; landed intentionally, not a stray carry-over — see
    `cross-repo/inbox/2026-08-05-example-cockpit-repo-em-wsc-leg-b-counts-spinoffs-as-live-children.md`.
    """
    captured: dict = {}

    async def _fake_handler(params, common_dir):
        captured.update(params)
        return {"exit_code": 1, "referenced": False}

    monkeypatch.setattr("coordinator_core.ipc.get_op_handler", lambda name: _fake_handler)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda root: root)

    result = wsc._dispatch_has_live_children(tmp_path, "state/handoffs/x.md")

    assert result["exit_code"] == 1
    edge_kinds = {k.strip() for k in captured["edge_kinds"].split(",")}
    assert edge_kinds == {"predecessor", "additional_predecessors"}
    assert "forked_from" not in edge_kinds


def test_consumed_handoff_completeness_clears_the_gate_when_all_boxes_ticked_and_no_live_child(monkeypatch, tmp_path):
    _write_ac_handoff(tmp_path, "state/handoffs/x.md", "## Acceptance criteria\n\n- [x] one\n- [x] two\n")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" not in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-consumed-handoff-completeness" not in _depends_on_list(wsc_tail)
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    assert gate_evidence["blocks"] is False
    assert gate_evidence["elements"][0]["leg_a"]["verdict"] == "clean"
    assert gate_evidence["elements"][0]["leg_b"]["verdict"] == "no-children"


def test_consumed_handoff_completeness_leg_a_open_blocks_wsc_tail(monkeypatch, tmp_path):
    _write_ac_handoff(tmp_path, "state/handoffs/x.md", "## Acceptance criteria\n\n- [x] one\n- [ ] two\n")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-consumed-handoff-completeness" in _depends_on_list(wsc_tail)
    jp = next(j for j in decision_object["judgment_points"] if j["id"] == "jp-consumed-handoff-completeness")
    # 2026-08-05-session-shape-attribution-structural-gate C3: the override
    # arm now resolves all six attribution/tail directives, not just
    # d-run-wsc-tail — see build_consumed_handoff_completeness_judgment_
    # point's own docstring for why d-reconcile-completion-commits is
    # load-bearing among the five newly-named ids.
    assert jp["dispositions"] == [
        {
            "value": "override-known-in-flight",
            "resolves": [
                "d-run-wsc-tail",
                "d-claim-plan-execution-lock",
                "d-stamp-plan-implemented",
                "d-harvest-deferrals-1",
                "d-complete-entry",
                "d-reconcile-completion-commits",
            ],
        },
        {"value": "stop-and-handoff", "resolves": []},
    ]
    assert jp["recommendation"] is None


def test_consumed_handoff_completeness_leg_b_live_child_blocks_wsc_tail(monkeypatch, tmp_path):
    """AC4 -- leg B alone (a live child) fires the gate even with a fully
    ticked checklist."""
    _write_ac_handoff(tmp_path, "state/handoffs/x.md", "## Acceptance criteria\n\n- [x] one\n")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 0, "referenced": True, "children": ["state/handoffs/y.md"]})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    assert gate_evidence["elements"][0]["leg_a"]["verdict"] == "clean"
    assert gate_evidence["elements"][0]["leg_b"]["verdict"] == "live-child"


def test_consumed_handoff_completeness_leg_b_exit_code_2_does_not_block_but_is_loud(monkeypatch, tmp_path):
    """AC5/AC3b -- exit_code=2 never blocks, but the op's own `error`
    string must land in `gates.*` evidence AND the gate's own Step-4
    `summary_line` -- silence must never represent 'not checked'."""
    _write_ac_handoff(tmp_path, "state/handoffs/x.md", "## Acceptance criteria\n\n- [x] one\n")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 2, "error": "enumeration incomplete — scan_errors present"})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" not in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    assert gate_evidence["blocks"] is False
    leg_b = gate_evidence["elements"][0]["leg_b"]
    assert leg_b["verdict"] == "indeterminate"
    assert leg_b["error"] == "enumeration incomplete — scan_errors present"
    assert "enumeration incomplete — scan_errors present" in gate_evidence["summary_line"]


def test_consumed_handoff_completeness_plural_one_of_two_fires_other_still_evaluated(monkeypatch, tmp_path):
    """AC6 -- the gate evaluates per element; one in-flight element blocks
    without suppressing evaluation of the others."""
    _write_ac_handoff(tmp_path, "state/handoffs/x.md", "## Acceptance criteria\n\n- [ ] one\n")
    _write_ac_handoff(tmp_path, "state/handoffs/y.md", "## Acceptance criteria\n\n- [x] one\n")
    _patch_gate(
        monkeypatch,
        _gate(
            "chain-terminal",
            consumed_handoff="state/handoffs/x.md",
            consumed_handoff_paths=("state/handoffs/x.md", "state/handoffs/y.md"),
        ),
    )
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    elements_by_handoff = {e["handoff"]: e for e in gate_evidence["elements"]}
    assert set(elements_by_handoff) == {"state/handoffs/x.md", "state/handoffs/y.md"}
    assert elements_by_handoff["state/handoffs/x.md"]["blocks"] is True
    assert elements_by_handoff["state/handoffs/y.md"]["blocks"] is False
    assert elements_by_handoff["state/handoffs/y.md"]["leg_a"]["verdict"] == "clean"


# Review: coordinatorcode-reviewer-c13e4663 Finding 4 — the new plural loop
# had no test proving `_resolve_handoff_path_str`'s archived-handoff branch
# (a real fleet condition) still resolves and evaluates an element.
def test_consumed_handoff_completeness_plural_resolves_archived_handoff(monkeypatch, tmp_path):
    """AC6 -- an element whose handoff has already been swept into
    `archive/handoffs/YYYY-MM/` still resolves and is evaluated, not
    treated as unreadable."""
    _write_ac_handoff(tmp_path, "archive/handoffs/2026-08/2026-08-01-x.md", "## Acceptance criteria\n\n- [ ] one\n")
    _patch_gate(
        monkeypatch,
        _gate(
            "chain-terminal",
            consumed_handoff="state/handoffs/2026-08-01-x.md",
            consumed_handoff_paths=("state/handoffs/2026-08-01-x.md",),
        ),
    )
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "open"
    assert leg_a["detail"] == "1 of 1 acceptance criteria unticked"


def test_consumed_handoff_completeness_leg_a_indeterminate_when_handoff_unreadable(monkeypatch, tmp_path):
    """AC3b hole 1/3 -- the path resolve/read itself fails inside the loop
    (no such file, and not archived either)."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/missing.md", consumed_handoff_paths=("state/handoffs/missing.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" not in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "indeterminate"
    assert leg_a["detail"] == "handoff unreadable"


# Review: coordinatorcode-reviewer-c13e4663 Finding 1 — a non-UTF-8 handoff
# raised UnicodeDecodeError (a ValueError subclass) out of brief() uncaught
# instead of degrading to leg A's "handoff unreadable" indeterminate.
def test_consumed_handoff_completeness_leg_a_indeterminate_when_handoff_non_utf8(monkeypatch, tmp_path):
    """AC3b hole 1/3, non-OSError variant -- a handoff on disk that isn't
    valid UTF-8 must degrade to indeterminate, not crash brief()."""
    handoff_path = tmp_path / "state/handoffs/x.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_bytes(b"---\nstatus: open\n---\n\n\xff\xfe not valid utf-8\n")
    _patch_gate(
        monkeypatch,
        _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)),
    )
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" not in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "indeterminate"
    assert leg_a["detail"] == "handoff unreadable"


def test_consumed_handoff_completeness_leg_a_indeterminate_when_no_acceptance_criteria_heading(monkeypatch, tmp_path):
    """AC3b hole 2/3 -- C3's parser returns None (heading absent)."""
    _write_ac_handoff(tmp_path, "state/handoffs/x.md", "just a body, no AC heading\n")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" not in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "indeterminate"
    assert leg_a["detail"] == "no ## Acceptance criteria heading"


def test_consumed_handoff_completeness_leg_a_indeterminate_when_heading_present_but_empty(monkeypatch, tmp_path):
    """AC3b hole 3/3 -- C3's parser returns total=0 (heading present, no
    checkboxes under it). This is NOT a legitimate zero-AC pass."""
    _write_ac_handoff(tmp_path, "state/handoffs/x.md", "## Acceptance criteria\n\nnothing to see here\n")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" not in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "indeterminate"
    assert leg_a["open"] == 0
    assert leg_a["total"] == 0


# ---------------------------------------------------------------------------
# Leg A, kind: session-handoff — cross-repo/inbox/2026-08-03-example-doctrine-repo-em-
# wsc-leg-a-session-handoff-kind-blind.md: that kind never carries its own
# `## Acceptance criteria` (0/34 in example-doctrine-repo's corpus, 0/22 in claude-klabauter's),
# so leg A joins its `deliverable_id` frontmatter to the governing plan's own
# `deliverable_id` instead — the retired `plan:` frontmatter pointer's
# replacement, per PM ruling R2 (docs/plans/2026-08-04-terminal-state-
# propagation-join-keys.md § C12) — falling back to the `not-applicable`
# verdict — distinct from `indeterminate` — everywhere the join doesn't lead
# anywhere, including when the joined plan's own `status:` is terminal
# (AC13, built as part of C12).
# ---------------------------------------------------------------------------


def _write_session_handoff(tmp_path: Path, rel_path: str, deliverable_id: str | None) -> None:
    handoff_path = tmp_path / rel_path
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    dlv_line = f"deliverable_id: {deliverable_id}\n" if deliverable_id is not None else ""
    handoff_path.write_text(
        f"---\nkind: session-handoff\nstatus: open\n{dlv_line}---\n\nbody\n", encoding="utf-8"
    )


def _write_session_handoff_plan(
    tmp_path: Path,
    rel_path: str,
    body: str,
    deliverable_id: str = "dlv-thing",
    status: str = "approved",
) -> None:
    plan_path = tmp_path / rel_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\nstatus: {status}\ndeliverable_id: {deliverable_id}\n---\n\n{body}\n", encoding="utf-8"
    )


def _leg_a_non_terminal_schema_statuses() -> list[str] | None:
    """Mirrors `test_leg_a_terminal_plan_status_covers_every_terminal_member_
    of_the_schema_enum`'s own schema-fetch mechanism (example-doctrine-repo HEAD `git show`,
    `None` on an unregistered/missing example-doctrine-repo repo -- the caller turns that into
    a `pytest.skip`), but for the complement set: every `plan.schema.json`
    `status` enum member NOT in `_LEG_A_TERMINAL_PLAN_STATUS`. Enum-pinned
    so a future schema change that reclassified e.g. `landed` cannot pass
    this suite silently -- only `draft` was previously exercised here."""
    doe_root = resolve_doe_root()
    if not doe_root:
        return None
    doe_repo = Path(doe_root)
    if not doe_repo.exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(doe_repo), "show", "HEAD:coordinator/schemas/plan.schema.json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    # Review: coordinator:code-reviewer -- mirror the terminal-arm test's
    # hard failure on a git-show error against a *present* example-doctrine-repo checkout;
    # only "no example-doctrine-repo repo" collapses to None/skip, not a broken checkout.
    assert result.returncode == 0, f"Cannot read example-doctrine-repo HEAD plan.schema.json: {result.stderr.strip()}"
    doe_plan_schema = json.loads(result.stdout)
    schema_enum = set(doe_plan_schema["properties"]["status"]["enum"])
    return sorted(schema_enum - wsc._LEG_A_TERMINAL_PLAN_STATUS)


_LEG_A_NON_TERMINAL_SCHEMA_STATUSES = _leg_a_non_terminal_schema_statuses()


@pytest.mark.parametrize(
    "status",
    _LEG_A_NON_TERMINAL_SCHEMA_STATUSES
    or [pytest.param("draft", marks=pytest.mark.skip(reason="example-doctrine-repo repo not registered/found on this machine"))],
)
def test_session_handoff_leg_a_open_when_joined_plan_status_not_terminal(monkeypatch, tmp_path, status):
    """AC3, the inversion: a resolved plan whose own `status:` is NOT in
    `_LEG_A_TERMINAL_PLAN_STATUS` now blocks -- the join is the sole
    discriminator; the plan body carries no AC heading at all, since the
    checkbox parse this verdict used to depend on is gone (AC1). Enum-pinned
    over the schema's non-terminal complement (see
    `_leg_a_non_terminal_schema_statuses`) rather than a single hardcoded
    `draft` example."""
    _write_session_handoff(tmp_path, "state/handoffs/x.md", "dlv-thing")
    _write_session_handoff_plan(tmp_path, "docs/plans/2026-08-03-thing.md", "no AC heading at all\n", status=status)
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" in jp_ids
    element = decision_object["gates"]["consumed_handoff_completeness"]["elements"][0]
    leg_a = element["leg_a"]
    assert leg_a["verdict"] == "open"
    assert leg_a["detail"] == (
        f"plan docs/plans/2026-08-03-thing.md: status '{status}' is not terminal — "
        "consumed predecessor's plan is not closed"
    )
    assert leg_a["open"] is None
    assert leg_a["total"] is None
    assert element["blocks"] is True


def test_session_handoff_leg_a_not_applicable_when_no_deliverable_id(monkeypatch, tmp_path):
    _write_session_handoff(tmp_path, "state/handoffs/x.md", None)
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" not in jp_ids
    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    assert gate_evidence["blocks"] is False
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "not-applicable"
    assert leg_a["detail"] == "kind: session-handoff carries no deliverable_id frontmatter"
    # not-applicable is non-blocking AND not indeterminate -- it must not
    # show up in the noisy "indeterminate:" summary tail either.
    assert "indeterminate:" not in gate_evidence["summary_line"]


def test_session_handoff_leg_a_not_applicable_when_deliverable_id_unresolved(monkeypatch, tmp_path):
    # No plan anywhere carries this deliverable_id -- the gate must reach a
    # verdict (AC12) without any docs/plans/ population at all, not raise.
    _write_session_handoff(tmp_path, "state/handoffs/x.md", "dlv-no-such-plan")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    assert gate_evidence["blocks"] is False
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "not-applicable"
    assert "does not resolve to exactly one docs/plans" in leg_a["detail"]


def test_session_handoff_leg_a_not_applicable_when_joined_plan_status_terminal(monkeypatch, tmp_path):
    """AC13, built as part of C12, not a follow-up: a joined plan whose own
    `status:` is terminal (`implemented`/`shipped`/`superseded`/`deferred`)
    resolves not-applicable -- the gate declines to re-open a plan its own
    repo already closed. No AC heading in the plan body: the checkbox
    parse this verdict used to be gated behind is gone (AC1); `status:`
    alone now decides not-applicable vs. open."""
    _write_session_handoff(tmp_path, "state/handoffs/x.md", "dlv-thing")
    _write_session_handoff_plan(
        tmp_path,
        "docs/plans/2026-08-03-thing.md",
        "no AC heading at all\n",
        status="implemented",
    )
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    assert gate_evidence["blocks"] is False
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "not-applicable"
    assert leg_a["detail"] == "plan docs/plans/2026-08-03-thing.md: status 'implemented' is terminal"


@pytest.mark.parametrize("terminal_status", ["shipped", "superseded", "deferred", "abandoned", "complete"])
def test_session_handoff_leg_a_not_applicable_for_every_terminal_status(monkeypatch, tmp_path, terminal_status):
    """AC5: `_LEG_A_TERMINAL_PLAN_STATUS` gained `abandoned`/`complete` on
    top of the retained `implemented`/`shipped`/`superseded`/`deferred`
    (the last exercised separately by the terminal-status test above)."""
    _write_session_handoff(tmp_path, "state/handoffs/x.md", "dlv-thing")
    _write_session_handoff_plan(
        tmp_path,
        "docs/plans/2026-08-03-thing.md",
        "no AC heading at all\n",
        status=terminal_status,
    )
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    leg_a = decision_object["gates"]["consumed_handoff_completeness"]["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "not-applicable"


def test_session_handoff_leg_a_reaches_verdict_without_plan_field(monkeypatch, tmp_path):
    """AC12: the gate reaches a verdict on a handoff carrying no `plan:`
    field at all (the field is retired -- this is every live handoff now).
    A real, resolvable, non-terminal-status plan still blocks; `plan:` is
    never read. No AC heading: under the inversion, `status:` alone
    decides -- there is no checkbox for the gate to fall back to (AC1)."""
    handoff_path = tmp_path / "state/handoffs/x.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        "---\nkind: session-handoff\nstatus: open\ndeliverable_id: dlv-thing\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert "plan:" not in handoff_path.read_text(encoding="utf-8")
    _write_session_handoff_plan(tmp_path, "docs/plans/2026-08-03-thing.md", "no AC heading at all\n")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    leg_a = decision_object["gates"]["consumed_handoff_completeness"]["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "open"


def test_non_session_handoff_kind_regression_still_indeterminate(monkeypatch, tmp_path):
    """Regression: a `roadmap-baton`/`spinoff` (or any non-session-handoff
    kind) must keep today's exact `indeterminate` semantics — only
    `kind: session-handoff` changes behavior."""
    handoff_path = tmp_path / "state/handoffs/x.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text("---\nkind: roadmap-baton\nstatus: open\n---\n\nno AC heading\n", encoding="utf-8")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" not in jp_ids
    leg_a = decision_object["gates"]["consumed_handoff_completeness"]["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "indeterminate"
    assert leg_a["detail"] == "no ## Acceptance criteria heading"


def test_leg_a_terminal_plan_status_covers_every_terminal_member_of_the_schema_enum():
    """AC6 -- presence-only parity, deliberately NOT set-equality and
    deliberately NOT derived from the schema's enum (plan's anti-scope):
    `plan.schema.json`'s `status` enum carries no terminality bit (its own
    `landed` description says explicitly that value is NOT terminal, per
    both `_LEG_A_TERMINAL_PLAN_STATUS`'s own comment above and
    `lifecycle_constants.PLAN_ORPHAN_TERMINAL_STATUS`'s docstring: this
    codebase's terminal-set partitions are deliberately independent and not
    expected to agree with one another or with the schema enum wholesale.
    The terminal subset asserted here (`implemented`/`deferred`/`abandoned`/
    `superseded`) is hand-authored from the schema's own prose, not derived
    mechanically from `enum`."""
    doe_root = resolve_doe_root()
    if not doe_root:
        pytest.skip("example-doctrine-repo repo not registered on this machine")
    doe_repo = Path(doe_root)
    if not doe_repo.exists():
        pytest.skip(f"example-doctrine-repo repo not found at {doe_repo}")

    result = subprocess.run(
        ["git", "-C", str(doe_repo), "show", "HEAD:coordinator/schemas/plan.schema.json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, f"Cannot read example-doctrine-repo HEAD plan.schema.json: {result.stderr.strip()}"
    doe_plan_schema = json.loads(result.stdout)
    schema_enum = set(doe_plan_schema["properties"]["status"]["enum"])

    # Hand-authored, not schema-derived: which members of the enum this
    # codebase considers terminal. `landed` is explicitly excluded per the
    # schema's own description ("NOT terminal"); `draft`/`reviewed`/
    # `approved`/`executing` are the in-flight states.
    terminal_members = {"implemented", "deferred", "abandoned", "superseded"}
    assert terminal_members <= schema_enum, (
        "This test's hand-authored terminal subset no longer matches "
        f"plan.schema.json's status enum ({schema_enum!r}) -- revisit."
    )
    missing = terminal_members - wsc._LEG_A_TERMINAL_PLAN_STATUS
    assert not missing, (
        f"_LEG_A_TERMINAL_PLAN_STATUS is missing schema-terminal status(es) {missing!r} -- "
        "the inversion makes their absence load-bearing (a joined plan at one of these "
        "statuses would falsely resolve 'open')."
    )


def test_consumed_handoff_completeness_fires_on_single_session_disposition_with_resolvable_consumed_handoff(
    monkeypatch, tmp_path
):
    """AC3's own oracle: keyed on 'a consumed handoff resolved on disk',
    NOT on disposition == PREDECESSOR_CONSUMED. A session that shipped a
    handoff straight from `awaiting_gate` (bypassing /pickup's consume
    transition) leaves no `consumed_by` stamp and can carry a
    single-session disposition despite a resolvable consumed handoff
    (state/lessons/2026-07-21-ship-a-chain-terminal-handoff-via-the-co-
    4dc2ff716f44.yaml) -- gating on disposition would reproduce that exact
    blind spot."""
    _write_ac_handoff(tmp_path, "state/handoffs/x.md", "## Acceptance criteria\n\n- [ ] one\n")
    _patch_gate(
        monkeypatch,
        _gate(
            "single-session",
            consumed_handoff_paths=("state/handoffs/x.md",),
        ),
    )
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-consumed-handoff-completeness" in jp_ids
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "jp-consumed-handoff-completeness" in _depends_on_list(wsc_tail)


# ---------------------------------------------------------------------------
# Generalized phantom-resolves-id guard -- the jp-coverage-verdict/d-tail
# bug was one instance of a class: a judgment point's disposition.resolves
# can name an id no directive this pass ever emits, in which case choosing
# that disposition silently resolves nothing. This sweep catches the NEXT
# one, not just today's fixed instance.
# ---------------------------------------------------------------------------

#: EMPTY BY CONSTRUCTION -- retired 2026-07-28, and it must stay empty.
#:
#: This set (and the prefix-matching branch it fed in `_resolves_id_is_
#: satisfiable`) was the root cause of a live silent-failure class, not a
#: convenience. Its premise -- "an exact-match check would false-fail a real,
#: correctly-wired directive" -- was exactly backwards: `apply`'s real gate
#: (`ceremony_common.apply_halt._directive_gate_open`) matches a `resolves`
#: entry against a directive id EXACTLY, never by prefix. A `resolves` naming
#: an unsuffixed base is therefore NOT correctly wired -- it names nothing,
#: the gate never opens, and the directive never fires.
#:
#: By modelling satisfiability more loosely than the code it guards, this
#: guard reported PASS on four genuinely-broken families (`d-add-lesson`,
#: `d-queue-append-lesson`, `d-flip-memo-status`, `d-freeze-and-dispatch-
#: review-partition`). Example-retrieval-repo-em hit the first one live on 2026-07-28:
#: `apply` exited 0, no directive reported an error, and the captured lesson
#: was simply never written to disk.
#:
#: Negative-spec: never re-introduce a looser satisfiability rule here than
#: `_directive_gate_open` itself implements. A guard weaker than the gate it
#: guards is worse than no guard -- it converts a loud bug into a green suite.
#: Runtime-computed ids are threaded in from the directive builder instead
#: (the `*_resolves_ids()` helpers in each `directives_*` module).
_DYNAMIC_SUFFIX_RESOLVES_BASES: frozenset[str] = frozenset()

#: `resolves` ids naming a step with NO backing `directives[]` entry at all,
#: by design. `d-render-final-summary` is Step 4's pure string-formatting
#: fan-in (`directives_commit_tail.render_final_summary`) -- that function's
#: own docstring states explicitly it is "Pure string formatting, no CLI, no
#: `directives[]` entry". Extend only with an equally specific reason, never
#: a blanket exemption.
_NO_DIRECTIVE_BACKING_RESOLVES_IDS = frozenset({"d-render-final-summary"})


def _resolves_id_is_satisfiable(resolves_id: str, emitted_directive_ids: set[str]) -> bool:
    """Exact membership ONLY -- deliberately identical to the real gate's
    own rule (`_directive_gate_open`), so this guard can never again pass a
    `resolves` id that the gate itself would refuse to match."""
    return resolves_id in emitted_directive_ids


def _sweep_directive_ids_and_resolves_ids(
    monkeypatch, tmp_path
) -> tuple[set[str], set[str], set[str]]:
    """Collects the union of every directive id `brief()` can emit, every
    id any judgment_point's disposition names in `resolves`, and every
    judgment_point id itself, across a representative sweep of the axes
    that gate the most judgment points (chain-terminal vs single-session,
    a captured lesson, a resolved memo disposition, a review partition,
    plus -- 2026-07-27 coverage-gap fix -- a real orientation cache +
    pinboard note, scratch candidates, unattributable files, flags, and a
    real on-disk governing plan reachable from the chain-terminal leg's
    consumed handoff) -- broad enough to surface both dynamic-suffix
    families and the fixed-id majority without re-deriving
    `test_workstream_complete_contract.py`'s full disk-fixture machinery
    (this test only needs id STRINGS, never a real on-disk CLI resolution).

    Coverage note (2026-07-27, code-reviewer finding on `20f924ff`): the
    original version of this sweep set `decisions={lessons,
    memo_dispositions, review, review_partition}` only -- four of the ~nine
    axes `_build_preserved_judgment_points` (`__init__.py`) actually gates
    on. `orientation_cache_exists`, `scratch_candidates`,
    `unattributable_files`, `flags`, and the governing-plan trio (gated on
    `governing_plan_present`, which needs a REAL on-disk plan reachable
    from the consumed handoff, not just a truthy decisions key) were never
    set, so `build_pinboard_note_content_judgment_point`'s `drafted`
    disposition -- a real, non-empty `resolves=["d-append-orientation-
    pinboard"]` -- was never exercised by this "generalized" guard despite
    its own docstring's "catches the NEXT one" claim. Fixed by widening the
    decisions payload to cover all axes and seeding a real handoff+plan on
    the chain-terminal leg. See `test_extended_sweep_covers_every_
    preserved_judgment_point` below for the code-derived guard against this
    same gap recurring silently.
    """
    # `d-complete-entry`'s gate (`directives_completion.completion_archive_
    # predicate`) checks for a real `archive/` dir on disk -- seed it so
    # this sweep's `resolves=["d-complete-entry"]` entries (Step 2.6/2.6b's
    # judgment points) have a real directive to match against, mirroring
    # `test_workstream_complete_contract.py`'s own `_seed_disk_fixtures`.
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    # Real governing plan + consumed handoff naming it, so the chain-
    # terminal leg's `governing_plan_present` is True and the plan-
    # reconcile trio (Step 2/2.4/2.4b judgment points) fires.
    plan_slug = "sweep-coverage-governing-plan"
    _write_plan(tmp_path, plan_slug)
    _write_handoff(tmp_path, "state/handoffs/x.md", f"docs/plans/{plan_slug}.md")

    directive_ids: set[str] = set()
    resolves_ids: set[str] = set()
    judgment_point_ids: set[str] = set()
    decisions = {
        "lessons": [
            {
                "title": "contract-test lesson",
                "body": "contract-test lesson body",
                "scope": "universal",
                "queue_title": "contract-test queue title",
                "queue_body": "contract-test queue body",
                "surface": "coordinator/tests/contract-test.py",
                "proposed_action": "contract-test proposed action",
                "change_kind": "wiki-append",
            }
        ],
        "memo_dispositions": [{"path": "state/memo-outbox/x.md", "decision": "actioned"}],
        "review": {
            "sha_range": "a..b",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        },
        "review_partition": {
            "range": "aaaaaaa..bbbbbbb",
            "slices": [{"slice_id": "s1", "paths": ["coordinator/tests/contract-test.py"]}],
            "integrator_spec_tsv": "state/review-trail/contract-test-spec.tsv",
        },
        "orientation_cache_exists": True,
        "pinboard_note": "contract-test pinboard note",
        "scratch_candidates": ["state/scratch/contract-test-scratch-file.md"],
        "unattributable_files": ["state/scratch/contract-test-unattributable-file.md"],
        "flags": ["contract-test flagged item"],
    }
    for gate in (
        _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()),
        _gate("single-session", consumed_handoff_paths=()),
    ):
        _patch_gate(monkeypatch, gate)
        decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
        directive_ids.update(d["id"] for d in decision_object["directives"])
        for jp in decision_object["judgment_points"]:
            judgment_point_ids.add(jp["id"])
            for disposition in jp["dispositions"]:
                resolves_ids.update(disposition.get("resolves", []))
    return directive_ids, resolves_ids, judgment_point_ids


def test_no_judgment_point_resolves_a_phantom_directive_id(monkeypatch, tmp_path):
    """The jp-coverage-verdict/d-tail regression, generalized: every id any
    judgment point's `resolves` names must correspond to a real emitted
    directive -- exactly, via a documented dynamic-suffix family, or an
    explicitly named no-directive-backing exception -- never a bare string
    that nothing in `directives[]` will ever match, which would make
    picking that disposition silently resolve nothing."""
    directive_ids, resolves_ids, _judgment_point_ids = _sweep_directive_ids_and_resolves_ids(monkeypatch, tmp_path)
    for resolves_id in sorted(resolves_ids):
        if resolves_id in _NO_DIRECTIVE_BACKING_RESOLVES_IDS:
            continue
        assert _resolves_id_is_satisfiable(resolves_id, directive_ids), (
            f"a judgment point disposition resolves {resolves_id!r}, which names no "
            "directive this sweep ever emits (directly, via a documented dynamic-"
            "suffix family, or a named no-directive-backing exception) -- picking "
            "that disposition would silently resolve nothing"
        )


#: `_build_preserved_judgment_points` gates on `jp.id == "jp-session-shape"`
#: (session-shape uncertainty), `"jp-coverage-verdict"` (the chain-end
#: coverage gate), `"jp-review-scale"` (the review-scale surfacing), and
#: `"jp-consumed-handoff-completeness"` (2026-08-05-session-shape-
#: attribution-structural-gate C3's completeness brake) via their own
#: dedicated builders (`build_session_shape_judgment_point`/
#: `build_coverage_judgment_point`/`build_review_scale_judgment_point`/
#: `build_consumed_handoff_completeness_judgment_point`) that live in
#: `__init__.py` itself, not `judgments.py` -- they're deliberately excluded
#: from this derived set, which only covers the 29 preserved points
#: `judgments.py` (C2f) owns. All four are already pinned by their own tests
#: above (`test_uncertain_session_shape_surfaces_untrusted_gate_judgment_
#: point_with_no_recommendation`, `test_chain_terminal_coverage_judgment_
#: point_carries_a_recommendation`, the `jp-review-scale` tests, and
#: `test_consumed_handoff_completeness_leg_a_open_blocks_wsc_tail`).
def _all_preserved_judgment_point_ids() -> set[str]:
    """Derives the full set of judgment_point ids `_build_preserved_
    judgment_points` can ever emit directly from `judgments.py`'s own
    `JUDGMENT_POINT_BUILDERS` registry -- the module's own canonical,
    exactly-29-entry tuple of the builders that census actually owns (see
    that tuple's own docstring) -- rather than a `dir()` name-pattern sweep.

    Review: code-reviewer -- a bare `dir(wsc._judgments)` sweep matching
    every `build_*_judgment_point`-named, module-local callable silently
    assumed every such function is zero-arg and always returns a dict (true
    of exactly the 29 census builders, at the time this helper was
    written). `judgments.build_no_commit_row_disposition_judgment_point`
    (C13, a later, deliberately non-census judgment point consumed
    directly by `apply.py` -- see that builder's own docstring) takes an
    optional `no_commit_row_ids` argument and returns `None` when it is
    empty, so the old sweep's zero-arg call crashed on `None["id"]` the
    moment that builder was added, even though it was never meant to be
    part of this census at all. Deriving the id set from the tuple itself
    is both the fix and closer to the docstring's own stated intent
    ("code-derived enumeration, not a hand-restated list") -- the tuple IS
    the code-derived source of truth for the 29, not a second copy of it."""
    return {builder()["id"] for builder in wsc._judgments.JUDGMENT_POINT_BUILDERS}


def test_extended_sweep_covers_every_preserved_judgment_point(monkeypatch, tmp_path):
    """Code-derived closure guard for the coverage gap the phantom-directive
    sweep above just had fixed by hand (2026-07-27 code-reviewer finding):
    rather than trusting that the sweep's `decisions` payload happens to
    still enumerate every gating axis `_build_preserved_judgment_points`
    reads, this test enumerates every judgment point `judgments.py` (C2f)
    can build -- directly from that module's own functions, never a second
    hand-restated list -- and asserts the sweep's two-leg run actually
    reaches every one of them. If a future judgment point is added behind
    a NEW decisions-gated axis this sweep doesn't set, this test fails
    loud, naming exactly which id was never reached, instead of silently
    leaving a fresh phantom-resolves-id blind spot for the test above to
    miss the way it missed the pinboard one."""
    _, _, judgment_point_ids = _sweep_directive_ids_and_resolves_ids(monkeypatch, tmp_path)
    expected_ids = _all_preserved_judgment_point_ids()
    missing = expected_ids - judgment_point_ids
    assert not missing, (
        f"the phantom-directive-id sweep's decisions payload never reaches {sorted(missing)} -- "
        "judgments.py can build these judgment points but no gating axis this sweep sets makes "
        "_build_preserved_judgment_points emit them, so any disposition.resolves id on one of "
        "these points is invisible to test_no_judgment_point_resolves_a_phantom_directive_id. "
        "Extend _sweep_directive_ids_and_resolves_ids's `decisions` (or its disk fixtures) to "
        "reach the missing judgment point(s)."
    )


# ---------------------------------------------------------------------------
# judgment_points[] — built via the shared constructors, offer-never-verdict
# ---------------------------------------------------------------------------


def _session_shape_jp_ids(monkeypatch, tmp_path, gate) -> list[str]:
    _patch_gate(monkeypatch, gate)
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    return [jp["id"] for jp in decision_object["judgment_points"]]


@pytest.mark.parametrize("status", ["indeterminate", "ambiguous"])
def test_uncertain_session_shape_surfaces_untrusted_gate_judgment_point_with_no_recommendation(
    monkeypatch, tmp_path, status
):
    """The two historical uncertainty statuses keep emitting the point — now
    read off `detection`, not off diagnostics wording."""
    _patch_gate(
        monkeypatch,
        _gate(
            "single-session",
            diagnostics=["WARN: disposition resolved single-session with Detector C ..."],
            consumed_handoff_paths=(),
            detection={"deciding_leg": "none", "detector_c_status": status},
        ),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    matches = [jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-session-shape"]
    assert len(matches) == 1
    assert matches[0]["recommendation"] is None


def test_detector_c_crash_recovery_match_surfaces_the_session_shape_judgment_point(monkeypatch, tmp_path):
    """The defect this seam exists to close (2026-08-05 plan, Defect 1).

    Detector C's single-match NOTE ("Single-overlap match on a N-entry
    scope — sanity-check before relying on it...") contains neither
    "indeterminate" nor "ambiguous", so the old prose-matching predicate
    returned False and `jp-session-shape` never emitted on ANY crash-recovery
    attribution — precisely the case where an EM has determined the
    attribution is coincidental and needs `SINGLE_SESSION` to be selectable.
    The diagnostic below is the producer's real wording, and it is the
    structured `detection` record, not that wording, that must fire this.

    The detection record carries the weak-breadth shape `_session_shape_
    is_uncertain` now requires: one matched entry out of a 2-entry scope, so
    the baton's other scope entry corroborates nothing."""
    _patch_gate(
        monkeypatch,
        _gate(
            "predecessor-consumed",
            consumed_handoff="state/handoffs/2026-07-17_160001_roadmap-sat-02.md",
            diagnostics=[
                "NOTE: chain-terminal resolved by Detector C (crash-recovery): this session "
                "committed against the scope of state/handoffs/2026-07-17_160001_roadmap-sat-02.md "
                "(matched via coordinator_core/x.py, 1 of 2 scope entries matched), whose "
                "claimer 5c844bd3 is not live. 1-of-2 scope-entry match — sanity-check before "
                "relying on it if that overlap could be coincidental (e.g. a widely-shared file)."
            ],
            detection={
                "deciding_leg": "detector-c",
                "detector_c_status": "crash-recovery",
                "matched_scope_entry_count": 1,
                "scope_size": 2,
                "single_match_kind": "exact",
            },
        ),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    matches = [jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-session-shape"]
    assert len(matches) == 1
    assert wsc.SINGLE_SESSION in [d["value"] for d in matches[0]["dispositions"]]


def test_session_shape_predicate_reads_detection_not_diagnostics_prose(monkeypatch, tmp_path):
    """The regression pin, and the point of the whole change: the predicate
    must be decidable with NO diagnostics at all, and undecidable from prose.

    Leg 1 — a status-bearing detection with an EMPTY diagnostics list still
    emits (nothing to substring-match, yet the answer is right).
    Leg 2 — diagnostics screaming both historical marker words, with an
    empty detection record, emit NOTHING. Any future re-coupling to prose
    fails one of these two.

    Leg 1's detection carries the full weak-breadth shape the predicate reads
    (single matched entry, 2-entry scope) precisely so the emit is caused by
    STRUCTURE with no prose available to substring-match."""
    assert "jp-session-shape" in _session_shape_jp_ids(
        monkeypatch,
        tmp_path,
        _gate(
            "predecessor-consumed",
            consumed_handoff="state/handoffs/x.md",
            diagnostics=[],
            detection={
                "deciding_leg": "detector-c",
                "detector_c_status": "crash-recovery",
                "matched_scope_entry_count": 1,
                "scope_size": 2,
                "single_match_kind": "exact",
            },
        ),
    )
    assert "jp-session-shape" not in _session_shape_jp_ids(
        monkeypatch,
        tmp_path,
        _gate(
            "single-session",
            diagnostics=[
                "WARN: liveness INDETERMINATE and the candidate set is AMBIGUOUS",
                "NOTE: indeterminate, ambiguous",
            ],
            consumed_handoff_paths=(),
            detection={"deciding_leg": "live-consume", "detector_c_status": None},
        ),
    )


def test_single_entry_prefix_scope_match_surfaces_but_exact_match_stays_quiet(
    monkeypatch, tmp_path
):
    """The breadth-not-count argument, pinned as a pair.

    Both gates below are `matched_scope_entry_count == 1` on a
    `scope_size == 1` baton — indistinguishable to any predicate keyed on
    entry COUNT. They differ only in `single_match_kind`:

      - "prefix" is the real occurrence that motivated the seam: a `scope:`
        entry naming a package directory (`coordinator_core/`) matches ANY
        session that touched the engine at all, so the attribution is
        coincidence-prone and must surface.
      - "exact" is the narrowest attribution the detector can make — one
        named file this session's own commit touched verbatim — and stays
        quiet.

    A future edit re-keying `_session_shape_is_uncertain` on entry count
    would pass every other session-shape test in this file and fail here."""
    prefix_gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-07-17_160001_roadmap-sat-02.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 1,
            "scope_size": 1,
            "single_match_kind": "prefix",
        },
    )
    assert "jp-session-shape" in _session_shape_jp_ids(monkeypatch, tmp_path, prefix_gate), (
        "a single-entry `scope: coordinator_core/` prefix hit matches any session that "
        "touched the engine — the coincidence-prone case this judgment point exists for"
    )

    exact_gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-07-17_160001_roadmap-sat-02.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 1,
            "scope_size": 1,
            "single_match_kind": "exact",
        },
    )
    assert "jp-session-shape" not in _session_shape_jp_ids(monkeypatch, tmp_path, exact_gate), (
        "a 1-entry scope matched EXACTLY is the narrowest attribution Detector C can make; "
        "flagging it re-fires the judgment point on every ordinary crash-recovery resolution"
    )


def test_all_prefix_multi_match_surfaces_example_market_data_repo_shape(monkeypatch, tmp_path):
    """The example-market-data-repo live-false-positive this fix exists to close
    (cross-repo/inbox/2026-08-06-example-market-data-repo-em-wsc-detector-c-
    false-consume-attribution.md): `matched_scope_entry_count=2`,
    `scope_size=7`, both matches bare directory prefixes (`tests/`,
    `docs/`), zero exact hits. The pre-fix short-circuit (`!= 1` -> not
    uncertain) read this as MORE corroborated than a single exact match --
    the opposite of the truth. `exact_match_count == 0` must flag it at ANY
    match count."""
    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-08-05_example_market_data_repo.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 2,
            "scope_size": 7,
            "single_match_kind": None,
            "exact_match_count": 0,
        },
    )
    assert "jp-session-shape" in _session_shape_jp_ids(monkeypatch, tmp_path, gate), (
        "two prefix-only matches out of a 7-entry scope corroborate nothing -- entry "
        "count must never stand in for corroboration"
    )


def test_one_exact_match_in_a_larger_scope_still_flagged_by_scope_size_rule(monkeypatch, tmp_path):
    """A single EXACT match (`exact_match_count == 1`) clears the new
    all-prefix check, but the pre-existing `scope_size >= 2` rule still
    fires -- the exact hit alone does not corroborate the rest of a 7-entry
    scope."""
    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-08-05_example_market_data_repo.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 1,
            "scope_size": 7,
            "single_match_kind": "exact",
            "exact_match_count": 1,
        },
    )
    assert "jp-session-shape" in _session_shape_jp_ids(monkeypatch, tmp_path, gate), (
        "one exact match out of a 7-entry scope is still coincidence-prone via the "
        "existing scope_size >= 2 rule"
    )


def test_scope_size_one_exact_match_stays_quiet_with_exact_match_count_present(monkeypatch, tmp_path):
    """The narrowest, most specific attribution Detector C can make -- a
    `scope_size == 1` EXACT match -- stays quiet even with the new
    `exact_match_count` field explicitly present and equal to 1. Non-
    negotiable per the fix spec."""
    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-08-05_example_market_data_repo.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 1,
            "scope_size": 1,
            "single_match_kind": "exact",
            "exact_match_count": 1,
        },
    )
    assert "jp-session-shape" not in _session_shape_jp_ids(monkeypatch, tmp_path, gate)


def test_exact_match_count_key_absent_degrades_to_todays_verdict(monkeypatch, tmp_path):
    """A stale copy of `wsc-session-disposition.py` that predates
    `exact_match_count` must degrade to EXACTLY today's (pre-fix)
    behaviour, never newly firing and never raising. Same input as the
    example-market-data-repo regression above, minus the new key -- and the
    verdict must be the pre-fix (quiet) one, because the old short-circuit
    (`matched_scope_entry_count != 1` -> not uncertain) is what a stale
    producer's consumer read replicates when the key is missing."""
    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-08-05_example_market_data_repo.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 2,
            "scope_size": 7,
            "single_match_kind": None,
            # exact_match_count deliberately absent -- stale-producer shape.
        },
    )
    assert "jp-session-shape" not in _session_shape_jp_ids(monkeypatch, tmp_path, gate), (
        "absent exact_match_count must not be confused with exact_match_count == 0"
    )


def test_one_exact_plus_two_prefix_matches_still_flags_the_near_neighbour_miss(monkeypatch, tmp_path):
    """The 2026-08-06 second-pass regression: gating the weak-single-exact
    case on `matched_scope_entry_count == 1` let extra worthless prefix
    hits SILENCE an already-flagged attribution. `exact_match_count == 1`
    and `scope_size >= 2` must flag regardless of `matched_scope_entry_
    count`."""
    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-08-05_example_market_data_repo.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 3,
            "scope_size": 7,
            "single_match_kind": None,
            "exact_match_count": 1,
        },
    )
    assert "jp-session-shape" in _session_shape_jp_ids(monkeypatch, tmp_path, gate), (
        "1 exact + 2 prefix in a 7-entry scope is no more corroborated than 1 exact alone "
        "-- adding prefix hits must not silence the flag"
    )


def test_two_exact_matches_in_a_7_entry_scope_stays_quiet(monkeypatch, tmp_path):
    """Two or more exact path matches is real corroboration and stays
    quiet, regardless of `matched_scope_entry_count` or accompanying
    prefix hits."""
    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-08-05_example_market_data_repo.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 4,
            "scope_size": 7,
            "single_match_kind": None,
            "exact_match_count": 2,
        },
    )
    assert "jp-session-shape" not in _session_shape_jp_ids(monkeypatch, tmp_path, gate)


def test_one_exact_match_in_a_1_entry_scope_stays_quiet_with_exact_match_count(monkeypatch, tmp_path):
    """The narrowest, most specific attribution possible -- `exact_match_
    count == 1` and `scope_size == 1` -- stays quiet."""
    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-08-05_example_market_data_repo.md",
        detection={
            "deciding_leg": "detector-c",
            "detector_c_status": "crash-recovery",
            "matched_scope_entry_count": 1,
            "scope_size": 1,
            "single_match_kind": "exact",
            "exact_match_count": 1,
        },
    )
    assert "jp-session-shape" not in _session_shape_jp_ids(monkeypatch, tmp_path, gate)


def _real_detector_c_detection(
    tmp_path: Path,
    scope_entries: list[str],
    committed_paths: list[str],
    dead_sid: str = "dead-sid",
) -> dict[str, object]:
    """Drives the REAL `_resolve_crash_recovery`/`_detection` producer
    functions (loaded from the actual `coordinator/bin/wsc-session-
    disposition.py` bin script, not re-implemented here) against a real
    on-disk handoff file, and returns the resulting `detection` dict --
    never a hand-typed `matched_scope_entry_count`/`scope_size`/
    `single_match_kind` triple. Every caller below feeds the real,
    COMPUTED numbers into `_gate(...)`, closing exactly the gap
    state/lessons/0000-00-00-green-tests-can-encode-the-bug-verify-
    producer-consumer-key.yaml names: a fixture hand-authored to match the
    reader proves only self-consistency."""
    handoff = tmp_path / "handoff.md"
    body = "predecessor: none\nscope:\n" + "".join(f"  - {e}\n" for e in scope_entries)
    handoff.write_text(body)
    diagnostics: list[str] = []
    outcome = _session_disposition._resolve_crash_recovery(  # noqa: SLF001 - real producer, deliberately
        [(str(handoff), dead_sid)], committed_paths, tmp_path, diagnostics
    )
    assert outcome[1] == "crash-recovery", (outcome, diagnostics)
    return _session_disposition._detection("detector-c", outcome[1], outcome.match_facts)  # noqa: SLF001


def test_disk_driven_directory_scope_prefix_match_surfaces_ac4_second_occurrence(monkeypatch, tmp_path):
    """AC4's second live occurrence, reconstructed from a REAL producer run
    (not a hand-authored detection dict): a baton whose entire `scope:` is
    ONE entry naming a package DIRECTORY (`coordinator_core/`), matched by
    prefix against a single committed path underneath it -- the shape that
    matched session cd272f17 against
    state/handoffs/2026-07-17_160001_roadmap-sat-02.md (plan Problem
    section, "second live occurrence" -- any session touching the engine
    matches this baton, which is structurally weaker than a multi-entry
    scope match despite carrying scope_size == 1)."""
    detection = _real_detector_c_detection(
        tmp_path,
        scope_entries=["coordinator_core/"],
        committed_paths=["coordinator_core/workstream_complete/__init__.py"],
    )
    assert detection["matched_scope_entry_count"] == 1
    assert detection["scope_size"] == 1
    assert detection["single_match_kind"] == "prefix"

    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-07-17_160001_roadmap-sat-02.md",
        detection=detection,
    )
    assert "jp-session-shape" in _session_shape_jp_ids(monkeypatch, tmp_path, gate), (
        "a single-entry scope: naming a package directory matches any session that "
        "touched the engine at all -- the coincidence-prone case AC4 exists for"
    )


def test_disk_driven_cockpit_incident_reconstruction_fires_session_shape_ac8(monkeypatch, tmp_path):
    """AC8: reconstructs cockpit's real positive from disk facts, not a
    hand-authored diagnostic string -- a handoff with a 4-entry scope, a
    commit touching exactly ONE of those entries, a claimer that is not
    live. Mirrors the origin incident (plan Problem section): one shared
    file (`GsdShell.tsx`) out of a 4-entry scope, claimed by a live session
    (reported not live here, matching the origin baton's own
    `deployment_state: in_flight`/non-live-claimer shape -- liveness
    resolution itself is `session-claim-cli`'s own, separately-tested
    seam; `_resolve_crash_recovery`'s caller is handed an already-stale
    baton, per that function's own docstring)."""
    detection = _real_detector_c_detection(
        tmp_path,
        scope_entries=[
            "src/components/GsdShell.tsx",
            "src/components/OtherA.tsx",
            "src/components/OtherB.tsx",
            "src/components/OtherC.tsx",
        ],
        committed_paths=["src/components/GsdShell.tsx"],
        dead_sid="5c844bd3-5fc6-49bb-ac3f-1685494781ca",
    )
    assert detection["matched_scope_entry_count"] == 1
    assert detection["scope_size"] == 4
    assert detection["single_match_kind"] == "exact"

    gate = _gate(
        "predecessor-consumed",
        consumed_handoff="state/handoffs/2026-08-04_140001_roadmap-uiux-01.md",
        detection=detection,
    )
    assert "jp-session-shape" in _session_shape_jp_ids(monkeypatch, tmp_path, gate), (
        "a single shared-file match out of a 4-entry scope, whose claimer is not live, "
        "is exactly the origin incident this judgment point exists to surface"
    )


def test_disk_driven_full_scope_match_stays_quiet_ac9(monkeypatch, tmp_path):
    """AC9: a full-scope match (every scope entry independently matched) is
    NOT coincidence-prone -- more than one matched entry corroborates the
    same baton -- pinned from the real producer, not an empty diagnostics
    list."""
    detection = _real_detector_c_detection(
        tmp_path,
        scope_entries=["a.py", "b.py"],
        committed_paths=["a.py", "b.py"],
    )
    assert detection["matched_scope_entry_count"] == 2
    assert detection["scope_size"] == 2

    gate = _gate("predecessor-consumed", consumed_handoff="state/handoffs/x.md", detection=detection)
    assert "jp-session-shape" not in _session_shape_jp_ids(monkeypatch, tmp_path, gate)


def test_disk_driven_partial_multi_entry_match_stays_quiet_ac9(monkeypatch, tmp_path):
    """AC9: a partial-but-multi-entry match (`len(matched_scope_entries) > 1
    and < scope_size`) is likewise not coincidence-prone -- pinned from the
    real producer, not the full-scope-match case alone."""
    detection = _real_detector_c_detection(
        tmp_path,
        scope_entries=["a.py", "b.py", "c.py"],
        committed_paths=["a.py", "b.py"],
    )
    assert detection["matched_scope_entry_count"] == 2
    assert detection["scope_size"] == 3

    gate = _gate("predecessor-consumed", consumed_handoff="state/handoffs/x.md", detection=detection)
    assert "jp-session-shape" not in _session_shape_jp_ids(monkeypatch, tmp_path, gate)


def test_disk_driven_single_entry_exact_scope_match_stays_quiet_ac9(monkeypatch, tmp_path):
    """AC9's third quiet case, disk-driven: a single EXACT-path match on a
    one-entry scope is a one-bit `single_match_kind` flip away from AC4's
    positive (the same one-entry-scope shape, but "prefix") -- the pairing
    this test pins against `test_single_entry_prefix_scope_match_surfaces_
    but_exact_match_stays_quiet` most needs producer-driven proof, since a
    hand-typed `detection` dict cannot catch that correspondence drifting.
    Review: coordinator:code-reviewer -- Finding 1, closes the one AC9 quiet
    case still hand-authored while its siblings were rebuilt via
    `_real_detector_c_detection`. Unaffected by the concurrent
    `matched_scope_entry_count` FILE-dedupe change in `wsc-session-
    disposition.py` -- a single entry matching a single file has no
    overlapping-entry double-count to dedupe either way."""
    detection = _real_detector_c_detection(
        tmp_path,
        scope_entries=["a.py"],
        committed_paths=["a.py"],
    )
    assert detection["matched_scope_entry_count"] == 1
    assert detection["scope_size"] == 1
    assert detection["single_match_kind"] == "exact"

    gate = _gate("predecessor-consumed", consumed_handoff="state/handoffs/x.md", detection=detection)
    assert "jp-session-shape" not in _session_shape_jp_ids(monkeypatch, tmp_path, gate)


@pytest.mark.parametrize("leg", ["env-override", "live-consume", "archive", "none"])
def test_confident_non_crash_recovery_resolution_surfaces_no_session_shape_judgment_point(
    monkeypatch, tmp_path, leg
):
    """AC9's clean case, keyed on a structurally clean gate rather than an
    empty diagnostics list: every deciding leg OTHER than Detector C
    resolved without a status, so there is nothing for an EM to confirm."""
    ids = _session_shape_jp_ids(
        monkeypatch,
        tmp_path,
        _gate(
            "single-session",
            diagnostics=["NOTE: chain-terminal resolved from archive (shipped/archived): x.md"],
            consumed_handoff_paths=(),
            detection={"deciding_leg": leg, "detector_c_status": None},
        ),
    )
    assert "jp-session-shape" not in ids


@pytest.mark.parametrize("leg", ["env-override", "live-consume", "archive"])
def test_stale_detector_c_status_on_a_non_detector_c_leg_does_not_fire(monkeypatch, tmp_path, leg):
    """Pins the `deciding_leg` guard `_session_shape_is_uncertain`'s first
    branch now enforces: `_detection()` in `wsc-session-disposition.py`
    never sets `detector_c_status` on `env-override`/`live-consume`/
    `archive`, but this predicate no longer merely trusts that invariant —
    it checks `deciding_leg` too. Before that tightening, a bare
    `detection.get("detector_c_status") in (...)` check would have fired
    on this record regardless of which leg decided, which is exactly the
    producer-contract leak this test exists to catch if it ever recurs."""
    ids = _session_shape_jp_ids(
        monkeypatch,
        tmp_path,
        _gate(
            "single-session",
            diagnostics=[],
            consumed_handoff_paths=(),
            detection={"deciding_leg": leg, "detector_c_status": "indeterminate"},
        ),
    )
    assert "jp-session-shape" not in ids


@pytest.mark.parametrize("leg", ["detector-c", "none"])
def test_indeterminate_detector_c_status_on_a_plausible_leg_still_fires(monkeypatch, tmp_path, leg):
    """Guards against over-tightening: narrowing the `deciding_leg` tuple in
    `_session_shape_is_uncertain`'s first branch down to nothing (or past
    the two legs that can plausibly carry `detector_c_status`) would make
    `test_stale_detector_c_status_on_a_non_detector_c_leg_does_not_fire`
    pass for the wrong reason. `detector-c` and `none` are the only two
    legs `_detection()` ever attaches a non-`None` `detector_c_status` to,
    and both must still surface the judgment point."""
    ids = _session_shape_jp_ids(
        monkeypatch,
        tmp_path,
        _gate(
            "single-session",
            diagnostics=[],
            consumed_handoff_paths=(),
            detection={"deciding_leg": leg, "detector_c_status": "indeterminate"},
        ),
    )
    assert "jp-session-shape" in ids


def test_clean_session_shape_surfaces_no_session_shape_judgment_point(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", diagnostics=[], consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = [jp["id"] for jp in decision_object["judgment_points"]]
    assert "jp-session-shape" not in ids


def test_session_shape_judgment_point_offers_canonical_and_legacy_dispositions_both_clearing_coverage_gate(
    monkeypatch, tmp_path
):
    """AC2b: `jp-session-shape`'s `dispositions[]` carries BOTH the canonical
    (`predecessor-consumed`) and legacy (`chain-terminal`) spellings as
    SEPARATE entries, each with the IDENTICAL `resolves=["d-coverage-gate"]`
    list — so `ceremony_common.apply_halt._disposition_resolves_directive`'s
    ordinary value-match clears `d-coverage-gate` for either spelling an EM
    types, with zero change to that cross-family shared predicate."""
    _patch_gate(
        monkeypatch,
        _gate(
            "single-session",
            diagnostics=["WARN: disposition resolved single-session with Detector C ..."],
            consumed_handoff_paths=(),
            detection={"deciding_leg": "none", "detector_c_status": "indeterminate"},
        ),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    matches = [jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-session-shape"]
    assert len(matches) == 1
    jp = matches[0]

    resolves_by_value = {entry["value"]: entry["resolves"] for entry in jp["dispositions"]}
    assert resolves_by_value.get("predecessor-consumed") == ["d-coverage-gate"]
    assert resolves_by_value.get("chain-terminal") == ["d-coverage-gate"]

    for chosen_value in ("predecessor-consumed", "chain-terminal"):
        assert apply_halt._disposition_resolves_directive(jp, chosen_value, "d-coverage-gate"), (
            f"choosing {chosen_value!r} must clear d-coverage-gate"
        )


def test_session_shape_judgment_point_offers_memo_predecessor_as_a_fourth_disposition(
    monkeypatch, tmp_path
):
    """AC4: `jp-session-shape`'s `dispositions[]` carries `memo-predecessor`
    as a fourth entry ALONGSIDE the pre-existing canonical/legacy/single-
    session three, whenever the point fires at all (not conditioned on
    which leg actually decided this resolution) — so an EM correcting a
    wrong Detector C attribution has the true answer available. Its
    `resolves` list is the SAME `["d-coverage-gate"]` as the canonical/
    legacy entries: forced by `_build_legacy_coverage_and_trail_directives`
    only building `d-coverage-gate` on `canonicalize(disposition) ==
    PREDECESSOR_CONSUMED` with a non-empty `gate.consumed_handoff`, which
    the memo leg never carries (plan § Problem (2))."""
    _patch_gate(
        monkeypatch,
        _gate(
            "single-session",
            diagnostics=["WARN: disposition resolved single-session with Detector C ..."],
            consumed_handoff_paths=(),
            detection={"deciding_leg": "none", "detector_c_status": "indeterminate"},
        ),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    matches = [jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-session-shape"]
    assert len(matches) == 1
    jp = matches[0]

    resolves_by_value = {entry["value"]: entry["resolves"] for entry in jp["dispositions"]}
    assert set(resolves_by_value.keys()) == {
        "predecessor-consumed",
        "chain-terminal",
        "single-session",
        "memo-predecessor",
    }
    assert resolves_by_value["memo-predecessor"] == ["d-coverage-gate"]
    assert apply_halt._disposition_resolves_directive(jp, "memo-predecessor", "d-coverage-gate"), (
        "choosing memo-predecessor must clear d-coverage-gate"
    )


def test_session_shape_is_uncertain_returns_false_for_a_memo_predecessor_detection_record(monkeypatch, tmp_path):
    """The settled-fact assertion (plan Execution Notes): a `memo-
    predecessor` deciding leg is NEVER flagged uncertain, even when its
    `.detection` record carries an `indeterminate`/`ambiguous` `detector_c_
    status` AND coincidence-prone Detector-C match facts — those fields ride
    along on the memo leg as diagnostics-only (Detector C's own status,
    surfaced for a human reader), never as this predicate's input. Both
    branches of `_session_shape_is_uncertain` require `deciding_leg` to be
    one of `("detector-c", "none")` / `== "detector-c"`; `"memo-predecessor"`
    matches neither, by construction — this pins that behaviour rather than
    re-deriving it, per this chunk's brief (do not edit the function body)."""
    coincidence_prone_detection = {
        "deciding_leg": "memo-predecessor",
        "detector_c_status": "ambiguous",
        "matched_scope_entry_count": 1,
        "scope_size": 3,
        "single_match_kind": "prefix",
    }
    assert wsc._session_shape_is_uncertain(coincidence_prone_detection) is False

    ids = _session_shape_jp_ids(
        monkeypatch,
        tmp_path,
        _gate(
            "memo-predecessor",
            diagnostics=[],
            consumed_handoff_paths=(),
            detection=coincidence_prone_detection,
        ),
    )
    assert "jp-session-shape" not in ids


def test_chain_terminal_coverage_judgment_point_carries_a_recommendation(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    matches = [jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-coverage-verdict"]
    assert len(matches) == 1
    recommendation = matches[0]["recommendation"]
    assert isinstance(recommendation, dict)
    assert set(recommendation.keys()) == {"disposition", "rationale"}
    assert recommendation["disposition"]
    assert recommendation["rationale"]


def test_single_session_has_no_coverage_judgment_point(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = [jp["id"] for jp in decision_object["judgment_points"]]
    assert "jp-coverage-verdict" not in ids


def test_review_dispatch_vehicle_choice_does_not_recommend_the_provisioning_bypassing_vehicle():
    """A recommended dispatch vehicle must not strip context an agent type
    declares as spawn-provided.

    Report sidecars are provisioned by a `PreToolUse` hook matched on the
    `Agent` tool; a Workflow-internal ``agent()`` spawn never traverses it,
    so a ``report_sidecar``-eligible reviewer arrives with no
    ``sidecar_path`` and refuses to review. Recommending `review-wave-
    workflow` once burned a full reviewer wave for zero findings. Both
    vehicles stay offered -- the Workflow one is usable with explicit
    pre-provisioning -- but it is not what an EM gets by default.
    """
    jp = judgments.build_review_dispatch_vehicle_choice_judgment_point()
    offered = {d["value"] for d in jp["dispositions"]}
    assert offered == {"hand-dispatch", "review-wave-workflow"}
    assert jp["recommendation"]["disposition"] == "hand-dispatch"
    assert "provision" in jp["recommendation"]["rationale"]


def test_all_judgment_points_carry_the_shared_constructor_shape(monkeypatch, tmp_path):
    _patch_gate(
        monkeypatch,
        _gate(
            "chain-terminal",
            consumed_handoff="state/handoffs/x.md",
            diagnostics=["WARN: ... AMBIGUOUS ..."],
            consumed_handoff_paths=(),
        ),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    assert decision_object["judgment_points"], "expected both judgment points to fire in this fixture"
    for jp in decision_object["judgment_points"]:
        assert set(jp.keys()) == {
            "id",
            "question",
            "dispositions",
            "evidence",
            "reason",
            "recommendation",
            "revalidate_at_dispatch",
            "round_trip",
        }


# ---------------------------------------------------------------------------
# Governing-plan resolution from the consumed handoff's own frontmatter
# (2026-07-27 regression fix) — the assembler previously only ever
# resolved a governing plan from caller-supplied `decisions` or the fixed
# `tasks/todo.md`/`tasks/plan.md` fallbacks, silently dropping the four
# plan-gated directives (claim/stamp/harvest) whenever an EM invoked
# `brief` without threading the handoff's own `governing_plan:` field
# through as a decision. See `directives_lessons_plan.resolve_governing_
# plan_with_source`'s docstring for the full precedence order this section
# pins.
# ---------------------------------------------------------------------------


def _write_plan(tmp_path, slug: str) -> None:
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / f"{slug}.md").write_text(f"# {slug}\n", encoding="utf-8")


def _write_handoff(tmp_path, rel_path: str, governing_plan_value: str) -> None:
    handoff_path = tmp_path / rel_path
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        f"---\nstatus: open\ngoverning_plan: {governing_plan_value}\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_handoff_frontmatter_resolves_governing_plan_when_no_decisions_supplied(monkeypatch, tmp_path):
    slug = "2026-07-26-workstream-complete-computed-frontage"
    _write_plan(tmp_path, slug)
    _write_handoff(tmp_path, "state/handoffs/x.md", f"docs/plans/{slug}.md")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-claim-plan-execution-lock" in ids
    assert "d-stamp-plan-implemented" in ids
    assert any(d["id"].startswith("d-harvest-deferrals-") for d in decision_object["directives"])
    assert decision_object["preflight"]["governing_plan_resolution"] == {
        "source": "handoff_frontmatter",
        "slug": slug,
    }


def test_decisions_slug_still_wins_over_handoff_frontmatter_field(monkeypatch, tmp_path):
    handoff_slug = "handoff-named-plan"
    decisions_slug = "decisions-named-plan"
    _write_plan(tmp_path, handoff_slug)
    _write_plan(tmp_path, decisions_slug)
    _write_handoff(tmp_path, "state/handoffs/x.md", f"docs/plans/{handoff_slug}.md")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={"governing_plan_slug": decisions_slug}, repo_root=tmp_path)
    assert decision_object["preflight"]["governing_plan_resolution"] == {
        "source": "decisions_slug",
        "slug": decisions_slug,
    }


def test_decisions_path_still_wins_over_handoff_frontmatter_field(monkeypatch, tmp_path):
    handoff_slug = "handoff-named-plan"
    decisions_slug = "decisions-path-plan"
    _write_plan(tmp_path, handoff_slug)
    _write_plan(tmp_path, decisions_slug)
    _write_handoff(tmp_path, "state/handoffs/x.md", f"docs/plans/{handoff_slug}.md")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    decision_object = wsc.brief(
        decisions={"governing_plan_path": f"docs/plans/{decisions_slug}.md"}, repo_root=tmp_path
    )
    assert decision_object["preflight"]["governing_plan_resolution"] == {
        "source": "decisions_path",
        "slug": decisions_slug,
    }


def test_handoff_frontmatter_naming_a_nonexistent_plan_resolves_none_not_a_fabricated_plan(monkeypatch, tmp_path):
    _write_handoff(tmp_path, "state/handoffs/x.md", "docs/plans/does-not-exist.md")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-claim-plan-execution-lock" not in ids
    assert "d-stamp-plan-implemented" not in ids
    assert decision_object["preflight"]["governing_plan_resolution"] == {
        "source": "handoff_frontmatter_not_found",
        "slug": None,
    }


def test_literal_string_null_governing_plan_field_treated_as_absent(monkeypatch, tmp_path):
    _write_handoff(tmp_path, "state/handoffs/x.md", "'null'")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-claim-plan-execution-lock" not in ids
    assert decision_object["preflight"]["governing_plan_resolution"]["source"] == "none"


def test_archived_consumed_handoff_still_resolves_governing_plan(monkeypatch, tmp_path):
    slug = "archived-consumed-handoff-plan"
    _write_plan(tmp_path, slug)
    # Simulate a concurrent boot sweep archiving the handoff out from under
    # this session while it remains the correct provenance record -- the
    # live path never exists; only the archive destination does.
    archived_path = tmp_path / "archive" / "handoffs" / "2026-07" / "2026-07-27-some-handoff.md"
    archived_path.parent.mkdir(parents=True, exist_ok=True)
    archived_path.write_text(
        f"---\nstatus: shipped\ngoverning_plan: docs/plans/{slug}.md\n---\n\nbody\n",
        encoding="utf-8",
    )
    _patch_gate(
        monkeypatch,
        _gate("chain-terminal", consumed_handoff="state/handoffs/2026-07-27-some-handoff.md", consumed_handoff_paths=()),
    )

    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-claim-plan-execution-lock" in ids
    assert decision_object["preflight"]["governing_plan_resolution"] == {
        "source": "handoff_frontmatter",
        "slug": slug,
    }


# ---------------------------------------------------------------------------
# Claim/release symmetry (2026-07-27 follow-up finding): the handoff-
# frontmatter fix above resolved `governing_plan` correctly for `d-claim-
# plan-execution-lock` but `d-release-plan-claim` was still built from the
# raw `decisions.get("governing_plan_slug")` — a lock taken via the
# handoff-frontmatter (or fixed-fallback) leg was therefore never released.
# This section pins the durable invariant rather than just the one call
# site: whenever the claim fires, the release must fire too, regardless of
# which precedence leg resolved the plan.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decisions_factory",
    [
        pytest.param(lambda slug: {}, id="handoff_frontmatter"),
        pytest.param(lambda slug: {"governing_plan_slug": slug}, id="decisions_slug"),
        pytest.param(lambda slug: {"governing_plan_path": f"docs/plans/{slug}.md"}, id="decisions_path"),
    ],
)
def test_claim_and_release_plan_directives_are_symmetric_under_every_resolution_source(
    monkeypatch, tmp_path, decisions_factory
):
    slug = "claim-release-symmetry-plan"
    _write_plan(tmp_path, slug)
    _write_handoff(tmp_path, "state/handoffs/x.md", f"docs/plans/{slug}.md")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions=decisions_factory(slug), repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-claim-plan-execution-lock" in ids, "expected the claim to fire for this fixture"
    assert "d-release-plan-claim" in ids, (
        "d-claim-plan-execution-lock fired but d-release-plan-claim did not -- "
        "a plan-execution lock would be taken and never released"
    )
    release_directive = next(d for d in decision_object["directives"] if d["id"] == "d-release-plan-claim")
    assert release_directive["args"][-2] == slug
    assert release_directive["args"][-1] == "{d-run-wsc-tail.landed}", (
        "d-release-plan-claim must carry the ordering-only producer-readiness "
        "token trailing the slug -- see apply.py's _resolve_arg_tokens '.landed' "
        "field"
    )


def test_no_release_plan_directive_when_no_governing_plan_resolved(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-claim-plan-execution-lock" not in ids
    assert "d-release-plan-claim" not in ids


# ---------------------------------------------------------------------------
# classify_session_authored_files -- the over-commit-hazard regression guard
# for jp-stage-paths-missing's evidence chain (state/bug-backlog/2026-07-29-
# workstream-complete-silently-under-commi-33e5cdf24112.yaml). This function
# itself is NOT modified by that fix -- only wired -- but its two documented
# safety fallthroughs (known_concurrent_paths exclusion, session_start_time
# =None degrades to nothing-authored) are exactly what stands between "offer
# a candidate list" and "stage a live peer session's files", so they get a
# direct regression test rather than relying only on the brief()-level
# jp-stage-paths-missing tests above to exercise them indirectly.
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def test_classify_session_authored_files_excludes_known_concurrent_paths(tmp_path):
    from datetime import datetime, timedelta, timezone

    from coordinator_core.workstream_complete import directives_memo_lifecycle

    _init_git_repo(tmp_path)
    (tmp_path / "peer-untracked.md").write_text("peer session's file\n", encoding="utf-8")

    session_start_time = datetime.now(timezone.utc) - timedelta(hours=1)

    excluded = directives_memo_lifecycle.classify_session_authored_files(
        tmp_path, session_start_time, known_concurrent_paths=frozenset({"peer-untracked.md"})
    )
    row = next(r for r in excluded if r["path"] == "peer-untracked.md")
    assert row["session_authored"] is False
    assert row["reason"] == "known-concurrent"

    not_excluded = directives_memo_lifecycle.classify_session_authored_files(
        tmp_path, session_start_time, known_concurrent_paths=frozenset()
    )
    row = next(r for r in not_excluded if r["path"] == "peer-untracked.md")
    assert row["session_authored"] is True, (
        "with no known_concurrent_paths exclusion, an untracked file with mtime after "
        "session_start_time must classify as session-authored under predicate (b) -- "
        "this is exactly the over-commit hazard the missing peer-exclusion producer "
        "creates, which is why jp-stage-paths-missing offers this set as evidence "
        "only and never auto-stages it"
    )


def test_classify_session_authored_files_with_none_start_time_classifies_nothing(tmp_path):
    from coordinator_core.workstream_complete import directives_memo_lifecycle

    _init_git_repo(tmp_path)
    (tmp_path / "some-untracked.md").write_text("content\n", encoding="utf-8")

    results = directives_memo_lifecycle.classify_session_authored_files(
        tmp_path, session_start_time=None, known_concurrent_paths=frozenset()
    )
    assert results, "expected the dirty untracked file to appear in the porcelain scan"
    for row in results:
        assert row["session_authored"] is False
        assert row["reason"] == "fails predicate (a) and (b)"


# ---------------------------------------------------------------------------
# directives_commit_tail.resolve_known_concurrent_paths -- the Step 3.0
# case-(b) peer-exclusion PRODUCER (the gap `classify_session_authored_
# files`'s `known_concurrent_paths` parameter documented but that, until
# now, nothing in this codebase ever computed -- every wired call passed
# `frozenset()`). See `directives_commit_tail.py`'s own docstring for the
# correctness bar these tests pin: never exclude THIS session's own paths,
# prefer over-exclusion when genuinely ambiguous, and never degrade an
# unreachable resolver into a confident empty answer.
# ---------------------------------------------------------------------------


def _make_live_session_claim_dir(repo_root: Path, sid: str) -> None:
    """A freshly-created, meta.json-less claim dir under the
    `coordinator-sessions/` hub -- `liveness.session_live`'s Layer 2
    meta-less fallback reads this dir's own (just-now) mtime as the
    recency source, which is always well under the 30-minute liveness
    boundary, so this reads LIVE without needing to fabricate a meta.json."""
    claim_dir = repo_root / ".git" / "coordinator-sessions" / sid
    claim_dir.mkdir(parents=True)


def _make_stale_session_claim_dir(repo_root: Path, sid: str) -> None:
    """A claim dir with a `meta.json` `last_activity` far enough in the
    past that `liveness.session_live`'s Layer 2 recency gate reads DEAD."""
    import json
    from datetime import datetime, timedelta, timezone

    claim_dir = repo_root / ".git" / "coordinator-sessions" / sid
    claim_dir.mkdir(parents=True)
    stale_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (claim_dir / "meta.json").write_text(
        json.dumps({"last_activity": stale_iso}), encoding="utf-8"
    )


def test_resolve_known_concurrent_paths_excludes_live_peer_untracked_file(tmp_path):
    from coordinator_core.workstream_complete import directives_commit_tail

    _init_git_repo(tmp_path)
    peer_sid = "11111111-1111-1111-1111-111111111111"
    _make_live_session_claim_dir(tmp_path, peer_sid)

    peer_dir = tmp_path / "state" / "subagent-share" / peer_sid
    peer_dir.mkdir(parents=True)
    (peer_dir / "peer-report.md").write_text("peer's in-progress work\n", encoding="utf-8")

    result = directives_commit_tail.resolve_known_concurrent_paths(tmp_path, "this-session-id")

    assert f"state/subagent-share/{peer_sid}/peer-report.md" in result


def test_resolve_known_concurrent_paths_never_excludes_this_sessions_own_paths(tmp_path):
    """The over-exclusion regression this producer must never reintroduce:
    THIS session's own claim dir/subagent-share files must never appear in
    its own known_concurrent_paths result, however plausible the
    enumeration otherwise looks."""
    from coordinator_core.workstream_complete import directives_commit_tail

    _init_git_repo(tmp_path)
    this_sid = "22222222-2222-2222-2222-222222222222"
    _make_live_session_claim_dir(tmp_path, this_sid)

    own_dir = tmp_path / "state" / "subagent-share" / this_sid
    own_dir.mkdir(parents=True)
    (own_dir / "mine.md").write_text("this session's own file\n", encoding="utf-8")

    result = directives_commit_tail.resolve_known_concurrent_paths(tmp_path, this_sid)

    assert f"state/subagent-share/{this_sid}/mine.md" not in result
    assert f"state/subagent-share/{this_sid}/" not in result


def test_resolve_known_concurrent_paths_empty_this_session_id_excludes_nothing(tmp_path):
    """An unresolvable caller identity must never be treated as license to
    exclude everything else found on disk -- see the producer's own
    docstring: without a resolved `this_session_id` there is no reliable
    way to keep our OWN claim dir out of the peer enumeration, so this
    degrades to `frozenset()` rather than risking self-exclusion."""
    from coordinator_core.workstream_complete import directives_commit_tail

    _init_git_repo(tmp_path)
    peer_sid = "33333333-3333-3333-3333-333333333333"
    _make_live_session_claim_dir(tmp_path, peer_sid)
    peer_dir = tmp_path / "state" / "subagent-share" / peer_sid
    peer_dir.mkdir(parents=True)
    (peer_dir / "peer-report.md").write_text("peer's file\n", encoding="utf-8")

    assert directives_commit_tail.resolve_known_concurrent_paths(tmp_path, "") == frozenset()


def test_resolve_known_concurrent_paths_stale_peer_does_not_cause_wholesale_exclusion(tmp_path):
    """A non-live/stale peer session must not be treated as concurrent (its
    files stay out of the exclusion set), and its mere presence alongside a
    genuinely live peer must not blow the live peer's own exclusion up into
    something broader than that one live peer's paths."""
    from coordinator_core.workstream_complete import directives_commit_tail

    _init_git_repo(tmp_path)
    live_peer_sid = "44444444-4444-4444-4444-444444444444"
    stale_peer_sid = "55555555-5555-5555-5555-555555555555"
    _make_live_session_claim_dir(tmp_path, live_peer_sid)
    _make_stale_session_claim_dir(tmp_path, stale_peer_sid)

    live_dir = tmp_path / "state" / "subagent-share" / live_peer_sid
    live_dir.mkdir(parents=True)
    (live_dir / "live.md").write_text("live peer file\n", encoding="utf-8")

    stale_dir = tmp_path / "state" / "subagent-share" / stale_peer_sid
    stale_dir.mkdir(parents=True)
    (stale_dir / "abandoned.md").write_text("stale peer file\n", encoding="utf-8")

    result = directives_commit_tail.resolve_known_concurrent_paths(tmp_path, "this-session-id")

    assert f"state/subagent-share/{live_peer_sid}/live.md" in result
    assert f"state/subagent-share/{stale_peer_sid}/abandoned.md" not in result


def test_resolve_known_concurrent_paths_degrades_conservatively_when_hub_unreadable(tmp_path, monkeypatch):
    """When the `coordinator-sessions/` claim-dir hub cannot be walked at
    all (permission failure, TOCTOU removal -- simulated here via a direct
    monkeypatch of the enumeration helper's `enumeration_reliable` return),
    this must NOT silently fall through to a confident empty set (the
    'unreachable resolver reads as unset' failure class) -- it falls back
    to the filesystem-only `state/subagent-share/` scan and treats every
    OTHER directory found there as a candidate peer unconditionally."""
    from coordinator_core.workstream_complete import directives_commit_tail

    _init_git_repo(tmp_path)

    abandoned_dir = tmp_path / "state" / "subagent-share" / "peer-x"
    abandoned_dir.mkdir(parents=True)
    (abandoned_dir / "abandoned.md").write_text("x\n", encoding="utf-8")

    own_dir = tmp_path / "state" / "subagent-share" / "this-session-id"
    own_dir.mkdir(parents=True)
    (own_dir / "mine.md").write_text("mine\n", encoding="utf-8")

    monkeypatch.setattr(
        directives_commit_tail,
        "_enumerate_peer_session_ids",
        lambda repo_root, this_session_id: (["ignored-because-unreliable"], False),
    )

    result = directives_commit_tail.resolve_known_concurrent_paths(tmp_path, "this-session-id")

    assert "state/subagent-share/peer-x/abandoned.md" in result
    assert "state/subagent-share/this-session-id/mine.md" not in result


def test_resolve_known_concurrent_paths_excludes_live_peer_committed_file(tmp_path):
    """Minimum-coverage bullet 2: a peer's own recent commit (Session-Id
    trailer keyed to the peer, landed since the peer's own session start)
    must be picked up via `_peer_committed_paths`, not just the peer's
    untracked subagent-share surface."""
    import subprocess

    from coordinator_core.workstream_complete import directives_commit_tail

    _init_git_repo(tmp_path)
    peer_sid = "66666666-6666-6666-6666-666666666666"
    _make_live_session_claim_dir(tmp_path, peer_sid)

    (tmp_path / "peer-committed.md").write_text("peer work\n", encoding="utf-8")
    subprocess.run(["git", "add", "peer-committed.md"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            "peer's own commit",
            "--trailer",
            f"Session-Id: {peer_sid}",
        ],
        cwd=tmp_path,
        check=True,
    )

    result = directives_commit_tail.resolve_known_concurrent_paths(tmp_path, "this-session-id")

    assert "peer-committed.md" in result


# ---------------------------------------------------------------------------
# preflight.decisions_template — AC1/AC2/AC3
# (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md)
# ---------------------------------------------------------------------------

#: The six `directives_*` submodules the free-value-key union is derived
#: from (AC3) — imported here, the same set `__init__.py`'s own
#: `_FREE_VALUE_KEY_SOURCES` composes, so this test can assert the template
#: against an independently-collected union rather than re-reading the
#: assembler's own private constant.
from coordinator_core.workstream_complete import directives_commit_tail as _dc_tail  # noqa: E402
from coordinator_core.workstream_complete import directives_completion as _dc_completion  # noqa: E402
from coordinator_core.workstream_complete import directives_lessons_plan as _dc_lessons  # noqa: E402
from coordinator_core.workstream_complete import directives_memo_lifecycle as _dc_memo  # noqa: E402
from coordinator_core.workstream_complete import directives_review as _dc_review  # noqa: E402
from coordinator_core.workstream_complete import directives_session_hygiene as _dc_hygiene  # noqa: E402
from coordinator_core.workstream_complete import directives_spine_worklist as _dc_spine_worklist  # noqa: E402

_DIRECTIVE_SUBMODULES = (
    _dc_completion,
    _dc_lessons,
    _dc_memo,
    _dc_tail,
    _dc_hygiene,
    _dc_review,
    _dc_spine_worklist,
)

#: Every module contributing a `FREE_VALUE_KEYS` constant to the template —
#: the six `directives_*` submodules PLUS the package `__init__` itself, whose
#: `brief()` body reads nine `decisions` keys (`review_partition` among them)
#: directly rather than through a submodule builder. The assembler is not
#: exempt from the one-oracle rule just because it is the aggregator.
_FREE_VALUE_KEY_MODULES = _DIRECTIVE_SUBMODULES + (wsc,)


def _expected_free_value_keys() -> set[str]:
    keys: set[str] = set()
    for module in _FREE_VALUE_KEY_MODULES:
        keys.update(module.FREE_VALUE_KEYS)
    return keys


def _jp_entries(template: dict) -> dict:
    """The subset of `decisions_template` keyed by a judgment-point id —
    identified structurally (a `{"disposition": ..., "options": ...}`
    shaped value), not by re-deriving the id set a second way."""
    return {
        k: v
        for k, v in template.items()
        if isinstance(v, dict) and set(v.keys()) == {"disposition", "options"}
    }


def test_decisions_template_covers_every_judgment_point_id_both_directions(monkeypatch, tmp_path):
    """AC1: every `judgment_points[].id` in the SAME envelope appears in
    `preflight.decisions_template` and vice versa (set equality, both
    directions), each valued `{"disposition": None, "options": [...]}`."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert jp_ids, "expected at least one judgment point on a single-session pass"

    template = decision_object["preflight"]["decisions_template"]
    templated_jp_ids = set(_jp_entries(template).keys())
    assert templated_jp_ids == jp_ids

    for jp in decision_object["judgment_points"]:
        entry = template[jp["id"]]
        assert entry["disposition"] is None
        assert entry["options"] == [d["value"] for d in jp["dispositions"]]


def test_decisions_template_free_value_keys_equal_union_of_module_constants(monkeypatch, tmp_path):
    """AC2/AC3: the template's non-judgment-point keys equal the UNION of
    each `directives_*` submodule's own `FREE_VALUE_KEYS` constant (AC3),
    every one valued `None` — never a hand-copied list, never a phantom
    key absent from every submodule's constant."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}

    template = decision_object["preflight"]["decisions_template"]
    free_value_keys_in_template = set(template.keys()) - jp_ids
    assert free_value_keys_in_template == _expected_free_value_keys()
    for key in free_value_keys_in_template:
        assert template[key] is None


def test_decisions_template_free_value_key_source_is_the_submodule_constants_not_a_restated_list():
    """AC3: `__init__.py` derives its free-value-key union by IMPORTING each
    submodule's `FREE_VALUE_KEYS` constant, not by hand-restating the key
    list a second time. Asserted two ways: (1) `wsc._FREE_VALUE_KEY_SOURCES`
    is literally each submodule's own constant object (identity-equal
    tuples, not a copy), and (2) each `directives_*.py` module declares
    `FREE_VALUE_KEYS` at module scope exactly once (a grep-shaped AST scan —
    two declarations would mean a stray second list crept back in)."""
    assert set(wsc._FREE_VALUE_KEY_SOURCES) == {  # noqa: SLF001 - the exact aggregation this AC guards
        module.FREE_VALUE_KEYS for module in _FREE_VALUE_KEY_MODULES
    }

    import ast
    import inspect

    for module in _FREE_VALUE_KEY_MODULES:
        tree = ast.parse(inspect.getsource(module))
        declarations = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "FREE_VALUE_KEYS":
                    declarations += 1
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "FREE_VALUE_KEYS":
                        declarations += 1
        assert declarations == 1, (
            f"{module.__name__} must declare FREE_VALUE_KEYS exactly once at module scope, "
            f"found {declarations}"
        )


def test_every_decisions_key_read_anywhere_in_the_package_is_discoverable_from_the_template(
    monkeypatch, tmp_path
):
    """AC2, generalized past the instance to the CLASS of defect.

    The AC2/AC3 pair above proves the template equals the union of the declared
    constants — but says nothing about whether those constants describe what the
    code actually READS. A key read via `decisions.get("x")` / `decisions["x"]`
    whose module forgot to add it to its own `FREE_VALUE_KEYS` satisfies both
    tests and is still invisible to a caller, which is the entire defect the
    template exists to close.

    This scan is the drift-catcher: it AST-walks every module in the package for
    a literal `decisions` subscript/`.get()` read and asserts the template
    carries that key. It fired for real — nine keys read directly by
    `__init__.py`'s own `brief()` body (`review_partition`, `pinboard_note`,
    `ubt_check`, `msg_file`, `flags`, `scratch_candidates`,
    `unattributable_files`, `orientation_cache_exists`,
    `classify_dispatch_plan_file`) were missing from the first implementation's
    template. `review_partition` was the costly one: it carries the review slice
    map whose absence left the `freeze-review-diff` directives blocked and
    hand-invoked in the run this plan reconstructs.

    A future key needs no test edit — declare it in the owning module's
    `FREE_VALUE_KEYS` and this passes; forget to, and it fails by name.
    """
    import ast
    import inspect

    read_keys: dict[str, set[str]] = {}
    for module in _FREE_VALUE_KEY_MODULES:
        found: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            # decisions["key"]
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "decisions"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                found.add(node.slice.value)
            # decisions.get("key")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "decisions"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
        if found:
            read_keys[module.__name__] = found

    assert read_keys, (
        "the AST scan found no `decisions` key reads anywhere in the package — "
        "the scan itself has broken (a refactor renamed the parameter?), not the code"
    )

    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    template = wsc.brief(decisions={}, repo_root=tmp_path)["preflight"]["decisions_template"]

    undiscoverable = {
        module_name: sorted(keys - set(template))
        for module_name, keys in read_keys.items()
        if keys - set(template)
    }
    assert not undiscoverable, (
        "these `decisions` keys are read by the code but absent from "
        f"preflight.decisions_template, so no caller can discover them: {undiscoverable} "
        "— add each to its OWN module's FREE_VALUE_KEYS constant, never to a second list"
    )


def test_decisions_template_lands_under_preflight_never_a_9th_envelope_key(monkeypatch, tmp_path):
    """AC7: the template lives under the free-form `preflight` sub-keys —
    never a 9th top-level envelope key."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    assert set(decision_object.keys()) == set(ENVELOPE_KEYS)
    assert "decisions_template" in decision_object["preflight"]


# ---------------------------------------------------------------------------
# 2026-07-30 example-doctrine-repo-em cross-repo memo (`cross-repo/archive/2026-07-30-
# example-doctrine-repo-em-wsc-review-trail-passthrough-and-memo-attribution.md`), item
# 1 -- directives_memo_lifecycle.compute_memo_resolution_attribution's three
# signals (picked_up_by / realized_by / archive_rename) and their union, plus
# judgments.build_memo_resolution_attribution_judgment_point's move from
# tier-3 untrusted-gate to tier-2 recommendation-carrying.
# ---------------------------------------------------------------------------


def _git(tmp_path: Path, *args: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(tmp_path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_compute_memo_resolution_attribution_picked_up_by_signal(tmp_path):
    from coordinator_core.workstream_complete import directives_memo_lifecycle as dml

    _init_git_repo(tmp_path)
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    sid = "session-abc-123"
    memo = archive_dir / "2026-07-30-test-memo.md"
    memo.write_text(
        f"---\ntitle: test\nstatus: actioned\npicked_up_by: {sid}\n---\n\nbody\n",
        encoding="utf-8",
    )

    records = dml.compute_memo_resolution_attribution(tmp_path, sid)
    row = next(r for r in records if r["basename"] == memo.name)
    assert row["signals"] == ["picked_up_by"]


def test_compute_memo_resolution_attribution_realized_by_signal(tmp_path):
    from coordinator_core.workstream_complete import directives_memo_lifecycle as dml

    _init_git_repo(tmp_path)
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "a session commit")
    sha = _git(tmp_path, "rev-parse", "HEAD")

    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    memo = archive_dir / "2026-07-30-test-memo.md"
    memo.write_text(
        f"---\ntitle: test\nstatus: actioned\nrealized_by: {sha[:8]}\n---\n\nbody\n",
        encoding="utf-8",
    )

    records = dml.compute_memo_resolution_attribution(tmp_path, "unrelated-sid")
    row = next(r for r in records if r["basename"] == memo.name)
    assert row["signals"] == ["realized_by"]


def test_compute_memo_resolution_attribution_archive_rename_signal(tmp_path):
    from coordinator_core.workstream_complete import directives_memo_lifecycle as dml

    _init_git_repo(tmp_path)
    inbox_dir = tmp_path / "cross-repo" / "inbox"
    archive_dir = tmp_path / "cross-repo" / "archive"
    inbox_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    memo_name = "2026-07-30-test-memo.md"
    inbox_path = inbox_dir / memo_name
    inbox_path.write_text("---\ntitle: t\nstatus: open\n---\n\nbody\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add memo")

    archive_path = archive_dir / memo_name
    _git(tmp_path, "mv", str(inbox_path), str(archive_path))
    _git(tmp_path, "commit", "-q", "-m", "action memo")

    records = dml.compute_memo_resolution_attribution(tmp_path, "unrelated-sid")
    row = next(r for r in records if r["basename"] == memo_name)
    assert "archive_rename" in row["signals"]


def test_compute_memo_resolution_attribution_union_of_multiple_signals(tmp_path):
    from coordinator_core.workstream_complete import directives_memo_lifecycle as dml

    _init_git_repo(tmp_path)
    sid = "session-abc-123"
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "a session commit")
    sha = _git(tmp_path, "rev-parse", "HEAD")

    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    memo = archive_dir / "2026-07-30-test-memo.md"
    memo.write_text(
        f"---\ntitle: test\nstatus: actioned\npicked_up_by: {sid}\nrealized_by: {sha[:8]}\n---\n\nbody\n",
        encoding="utf-8",
    )

    records = dml.compute_memo_resolution_attribution(tmp_path, sid)
    row = next(r for r in records if r["basename"] == memo.name)
    assert set(row["signals"]) == {"picked_up_by", "realized_by"}


def test_compute_memo_resolution_attribution_no_signal_omits_the_memo(tmp_path):
    from coordinator_core.workstream_complete import directives_memo_lifecycle as dml

    _init_git_repo(tmp_path)
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)
    memo = archive_dir / "2026-07-30-test-memo.md"
    memo.write_text(
        "---\ntitle: test\nstatus: actioned\npicked_up_by: some-other-session\n---\n\nbody\n",
        encoding="utf-8",
    )

    records = dml.compute_memo_resolution_attribution(tmp_path, "session-abc-123")
    assert memo.name not in {r["basename"] for r in records}


def test_memo_resolution_attribution_judgment_point_recommends_not_resolved_with_no_signals():
    from coordinator_core.contract.decision_object.judgment import _validate_recommendation
    from coordinator_core.workstream_complete import judgments as _judgments

    jp = _judgments.build_memo_resolution_attribution_judgment_point([], [])
    assert jp["id"] == "memo-resolution-attribution"
    # still a live judgment point -- not auto-resolved, still asks.
    assert jp["question"]
    assert {d["value"] for d in jp["dispositions"]} == {"resolved", "not-resolved"}
    assert jp["recommendation"]["disposition"] == "not-resolved"
    _validate_recommendation(jp["recommendation"])  # raises on shape violation


def test_memo_resolution_attribution_judgment_point_recommends_resolved_with_signals():
    from coordinator_core.contract.decision_object.judgment import _validate_recommendation
    from coordinator_core.workstream_complete import judgments as _judgments

    signals = [{"path": "/x/cross-repo/archive/a.md", "basename": "a.md", "signals": ["picked_up_by"]}]
    jp = _judgments.build_memo_resolution_attribution_judgment_point(["d-flip-memo-status:a.md"], signals)
    assert jp["question"]
    assert jp["recommendation"]["disposition"] == "resolved"
    _validate_recommendation(jp["recommendation"])
    assert "a.md" in jp["evidence"]
    assert "picked_up_by" in jp["evidence"]
    resolved_disposition = next(d for d in jp["dispositions"] if d["value"] == "resolved")
    assert resolved_disposition["resolves"] == ["d-flip-memo-status:a.md"]


# ---------------------------------------------------------------------------
# 2026-07-30 example-doctrine-repo-em cross-repo memo, item 2 -- an unrecognized flat
# `review_*` key on `decisions` now gets a loud stderr diagnostic instead of
# a silent drop; a legitimately absent `review` dict stays quiet.
# ---------------------------------------------------------------------------


def test_build_close_tail_args_directive_warns_on_unrecognized_flat_review_keys(capsys):
    decisions = {
        "review_sha_range": "a..b",
        "review_reviewer": "someone",
        "review_scope": "chain",
        "review_verdict": "ok",
        "review_diff_loc": 10,
    }
    directive = _dc_tail.build_close_tail_args_directive(decisions)
    captured = capsys.readouterr()
    assert captured.out == "", "the diagnostic must never touch stdout -- it is a parsed argv/token channel"
    assert "review_sha_range" in captured.err
    assert "review_reviewer" in captured.err
    assert "--review-sha-range" not in directive["args"], (
        "flat review_* keys are still not composed into --review-* flags -- the diagnostic "
        "supplements the silent drop, it does not add a second accepted spelling"
    )


def test_build_close_tail_args_directive_no_diagnostic_when_review_absent(capsys):
    directive = _dc_tail.build_close_tail_args_directive({})
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
    assert directive["args"] == ["tail-args"]


def test_build_close_tail_args_directive_no_diagnostic_when_review_present_correctly(capsys):
    decisions = {
        "review": {
            "sha_range": "a..b",
            "reviewer": "someone",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        }
    }
    directive = _dc_tail.build_close_tail_args_directive(decisions)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
    assert "--review-sha-range" in directive["args"]


# ---------------------------------------------------------------------------
# 2026-08-03 example-doctrine-repo-em-wsc-tail-review-metadata-dropped -- the transport
# hole: `build_wsc_tail_directive` documented that `depends_on=
# "d-close-tail-args"` spliced the producer's stdout into this directive's
# argv, but no token ever expressed that splice. `apply._resolve_arg_tokens`'s
# `.argv` field is the fix; these two pin the builder side (the regression
# anchor) and the end-to-end shape (the original hole, closed).
# ---------------------------------------------------------------------------


def test_build_wsc_tail_directive_args_end_with_the_close_tail_args_argv_token():
    directive = _dc_tail.build_wsc_tail_directive("abcdef", {"governing_plan_slug": "some-plan"})
    assert directive["args"][-1] == "{d-close-tail-args.argv}", (
        "d-run-wsc-tail must carry the explicit '.argv' token that transports "
        "d-close-tail-args's spliced --deleted-paths/--kept-entries/--review-* "
        "flags -- depends_on alone only orders the two directives, it does not "
        "thread the value (apply._resolve_arg_tokens's '.argv' field)"
    )
    assert directive["depends_on"] == "d-close-tail-args"


def test_review_flags_reach_the_wsc_tail_argv_token_transport():
    """The ORIGINAL end-to-end hole, closed: given a full nested `review`
    dict, `build_close_tail_args_directive` emits the five `--review-*`
    flags AND `build_wsc_tail_directive` carries the `.argv` token that
    transports them -- both builder halves of the wire, asserted together
    (an apply-level dispatch test lives in test_apply.py, which exercises
    the actual token expansion through `_execute_directives`)."""
    decisions = {
        "review": {
            "sha_range": "abc123..def456",
            "reviewer": "staff-eng",
            "scope": "chain",
            "verdict": "approved",
            "diff_loc": 42,
        },
        "deleted_paths": ["state/old-file.md"],
        "kept_entries": ["archive/completed/2026-07/kept.md"],
    }
    close_tail_args = _dc_tail.build_close_tail_args_directive(decisions)
    wsc_tail = _dc_tail.build_wsc_tail_directive("abcdef", decisions)

    assert close_tail_args["id"] == "d-close-tail-args"
    assert "--deleted-paths" in close_tail_args["args"]
    assert "--kept-entries" in close_tail_args["args"]
    assert "--review-sha-range" in close_tail_args["args"]
    assert "--review-reviewer" in close_tail_args["args"]
    assert "--review-scope" in close_tail_args["args"]
    assert "--review-verdict" in close_tail_args["args"]
    assert "--review-diff-loc" in close_tail_args["args"]

    assert wsc_tail["depends_on"] == close_tail_args["id"]
    assert wsc_tail["args"][-1] == "{d-close-tail-args.argv}"


# ---------------------------------------------------------------------------
# Lesson capture — structured facets survive the assembler
# ---------------------------------------------------------------------------


def _lesson_add_cli_path() -> Path:
    return Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-lesson-add"


def _cli_optional_value_flags(source: str) -> set[str]:
    """Every optional, value-carrying long flag `coordinator-lesson-add`'s
    argparse accepts — i.e. the flags a caller may legitimately supply and
    therefore the assembler must be able to forward. Required flags and
    store_true switches are excluded: the former are unconditional, the
    latter carry no author-composed content."""
    flags: set[str] = set()
    for block in source.split("parser.add_argument(")[1:]:
        block = block.split("\n    )")[0]
        if "required=True" in block or 'action="store_true"' in block:
            continue
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith('"--'):
                flags.add(stripped.split('"')[1])
                break
    return flags


def test_lesson_add_directive_forwards_every_structured_facet():
    """The facets are the half of a lesson that makes it actionable later;
    an assembler that can only carry title/body/scope silently drops them."""
    lesson = {
        "title": "t", "body": "b", "scope": "project",
        "trigger": "the trigger", "why": "the why", "how_to_apply": "the how",
        "target_wiki": "some-wiki.md", "proposed_target": "some-surface",
        "evidence": "docs/plans/some-plan.md",
    }
    args = _dc_lessons.build_lesson_capture_directives({"lessons": [lesson]})[0]["args"]
    for flag, value in (
        ("--trigger", "the trigger"),
        ("--why", "the why"),
        ("--how-to-apply", "the how"),
        ("--target-wiki", "some-wiki.md"),
        ("--proposed-target", "some-surface"),
        ("--evidence", "docs/plans/some-plan.md"),
    ):
        assert flag in args, f"{flag} dropped by the assembler"
        assert args[args.index(flag) + 1] == value


def test_lesson_add_directive_omits_absent_and_empty_facets():
    args = _dc_lessons.build_lesson_capture_directives(
        {"lessons": [{"title": "t", "body": "b", "scope": "project", "why": ""}]}
    )[0]["args"]
    assert args == ["--title", "t", "--body", "b", "--scope", "project"]


def test_assembler_covers_every_optional_flag_the_lesson_cli_accepts():
    """The drift guard: a facet flag added to the CLI but not to
    `_LESSON_OPTIONAL_FLAGS` is unreachable through the ceremony, and the
    only way an author gets it onto disk is by bypassing the directive."""
    cli = _lesson_add_cli_path()
    assert cli.is_file(), f"lesson-add CLI not found at {cli}"
    forwarded = {flag for _key, flag in _dc_lessons._LESSON_OPTIONAL_FLAGS}
    missing = _cli_optional_value_flags(cli.read_text(encoding="utf-8")) - forwarded
    assert not missing, (
        f"coordinator-lesson-add accepts {sorted(missing)} but the assembler cannot "
        "forward them -- add each to _LESSON_OPTIONAL_FLAGS"
    )


# ---------------------------------------------------------------------------
# AC3 — section-scoped acceptance-criteria checkbox parser
# ---------------------------------------------------------------------------


def test_acceptance_criteria_boxes_under_the_heading_are_counted():
    text = "\n".join(
        [
            "## Acceptance criteria",
            "- [x] AC1: done thing",
            "- [ ] AC2: open thing",
            "- [x] AC3: also done",
        ]
    )
    result = _dc_hygiene.parse_consumed_handoff_acceptance_criteria(text)
    assert result == {"done": 2, "total": 3, "open": 1}


def test_acceptance_criteria_boxes_outside_the_heading_are_ignored():
    text = "\n".join(
        [
            "- [ ] not under any AC heading",
            "## Acceptance criteria",
            "- [x] AC1: counted",
            "## Next section",
            "- [ ] not counted either",
        ]
    )
    result = _dc_hygiene.parse_consumed_handoff_acceptance_criteria(text)
    assert result == {"done": 1, "total": 1, "open": 0}


def test_acceptance_criteria_heading_absent_returns_none():
    text = "\n".join(["## Some other heading", "- [ ] a box, but not under AC"])
    assert _dc_hygiene.parse_consumed_handoff_acceptance_criteria(text) is None


def test_acceptance_criteria_heading_present_but_empty_returns_total_zero():
    text = "\n".join(["## Acceptance criteria", "", "Nothing here but prose.", "## Next"])
    result = _dc_hygiene.parse_consumed_handoff_acceptance_criteria(text)
    assert result == {"done": 0, "total": 0, "open": 0}


def test_acceptance_criteria_nested_subheading_terminates_the_section():
    """A same-or-higher heading after a deeper nested one still terminates
    the section at the FIRST such heading -- a deeper subheading nested
    directly under the AC heading does not itself end the section, but the
    next same-or-higher heading that follows it does."""
    text = "\n".join(
        [
            "## Acceptance criteria",
            "- [x] AC1: counted",
            "### A nested subheading, still inside the section",
            "- [ ] AC2: still counted, nested heading is deeper",
            "## Next top-level section",
            "- [x] not counted, past the boundary",
        ]
    )
    result = _dc_hygiene.parse_consumed_handoff_acceptance_criteria(text)
    assert result == {"done": 1, "total": 2, "open": 1}


def test_acceptance_criteria_batch_spelling_matches():
    text = "\n".join(
        [
            "## Acceptance criteria (batch)",
            "- [ ] AC1: open",
            "- [x] AC2: done",
        ]
    )
    result = _dc_hygiene.parse_consumed_handoff_acceptance_criteria(text)
    assert result == {"done": 1, "total": 2, "open": 1}


# ---------------------------------------------------------------------------
# gates.open_spine_row_worklist — docs/plans/2026-08-05-wsc-open-spine-row-
# worklist.md, chunks C1-C3. Mirrors the completeness-checklist gate's own
# test shape above; covers every "## Test surface" row: fires, silent
# (no open rows / no resolvable plan / MALFORMED spine), never blocks
# (AC4), and waiver keys present in decisions_template (AC6).
# ---------------------------------------------------------------------------


def _write_plan_with_spine(tmp_path: Path, slug: str, rows_yaml: str) -> None:
    plan_path = tmp_path / "docs" / "plans" / f"{slug}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "---\ntitle: \"a plan\"\nstatus: draft\n---\n\n"
        "# a plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows_yaml + "\n```\n",
        encoding="utf-8",
    )


def test_open_spine_row_gate_fires_and_names_every_open_row_with_five_exits(monkeypatch, tmp_path):
    """AC2: >=1 open row -> `applies` true, every open row's id/title pair
    named in `warn_text`, the five exits stated verbatim (the two
    PM-gated ones marked, plus the runnable `plan-tasks-resolve`
    command) per the five-exits ruling
    (cross-repo/inbox/2026-08-05-example-doctrine-repo-em-plan-tasks-five-exits-
    ruling.md)."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(
        tmp_path,
        "some-plan",
        "- id: C1\n"
        "  title: First open row\n"
        "  disposition: open\n"
        "- id: C2\n"
        "  title: Second open row\n"
        "  disposition: open\n",
    )

    decision_object = wsc.brief(decisions={"governing_plan_slug": "some-plan"}, repo_root=tmp_path)
    gate = decision_object["gates"]["open_spine_row_worklist"]

    assert gate["applies"] is True
    assert gate["open_count"] == 2
    assert gate["warn_text"] is not None
    assert "C1 — First open row" in gate["warn_text"]
    assert "C2 — Second open row" in gate["warn_text"]
    assert "coordinator/bin/plan-tasks-resolve" in gate["warn_text"]
    assert "disposition: coded" in gate["warn_text"]
    assert "disposition: spun_off" in gate["warn_text"]
    assert "disposition: backlogged" in gate["warn_text"]
    assert "disposition: wont_do" in gate["warn_text"]
    assert "PM word required" in gate["warn_text"]
    assert "no PM word needed" in gate["warn_text"]
    assert "carried on the successor baton" in gate["warn_text"]
    assert gate["summary_line"]


def test_open_spine_row_gate_silent_when_no_open_rows(monkeypatch, tmp_path):
    """AC3, leg 1: every row terminal -> silent no-op."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(
        tmp_path,
        "closed-plan",
        "- id: C1\n  title: Shipped row\n  disposition: coded\n  disposition_ref: abc123\n",
    )

    decision_object = wsc.brief(decisions={"governing_plan_slug": "closed-plan"}, repo_root=tmp_path)
    gate = decision_object["gates"]["open_spine_row_worklist"]

    assert gate["applies"] is False
    assert gate["warn_text"] is None
    assert gate["summary_line"]


def test_open_spine_row_gate_silent_when_no_resolvable_plan(monkeypatch, tmp_path):
    """AC3, leg 2: no governing plan resolves at all -> silent no-op."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    gate = decision_object["gates"]["open_spine_row_worklist"]

    assert gate["applies"] is False
    assert gate["warn_text"] is None
    assert gate["summary_line"]


def test_open_spine_row_gate_silent_on_malformed_spine_never_raises(monkeypatch, tmp_path):
    """AC7: a `load_rows` non-LOCATED status (here: malformed YAML in the
    fence) degrades to `applies: False` -- never raises out of the
    ceremony. `wsc.brief` completing at all is itself the assertion that
    nothing raised."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(tmp_path, "malformed-plan", "not: [valid, yaml, - broken")

    decision_object = wsc.brief(decisions={"governing_plan_slug": "malformed-plan"}, repo_root=tmp_path)
    gate = decision_object["gates"]["open_spine_row_worklist"]

    assert gate["applies"] is False
    assert gate["warn_text"] is None


def test_open_spine_row_gate_never_blocks_even_while_firing(monkeypatch, tmp_path):
    """AC4, the binding constraint: a firing gate contributes no blocking
    judgment point and the ceremony still completes with the SAME
    directives/judgment-point verdicts it would have computed with the
    gate silent -- proven by diffing two `brief()` runs against the SAME
    governing-plan slug/path (so every OTHER builder's inputs are
    byte-identical between runs), the only difference being the plan's
    own spine content: one open row vs. zero. Everything except
    `gates.open_spine_row_worklist` itself must be identical."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {"governing_plan_slug": "toggle-plan", "subject": "x"}

    _write_plan_with_spine(
        tmp_path,
        "toggle-plan",
        "- id: C1\n  title: Still open\n  disposition: open\n",
    )
    firing = wsc.brief(decisions=decisions, repo_root=tmp_path)
    assert firing["gates"]["open_spine_row_worklist"]["applies"] is True

    _write_plan_with_spine(
        tmp_path,
        "toggle-plan",
        "- id: C1\n  title: Now shipped\n  disposition: coded\n  disposition_ref: abc123\n",
    )
    silent = wsc.brief(decisions=decisions, repo_root=tmp_path)
    assert silent["gates"]["open_spine_row_worklist"]["applies"] is False

    def _strip(decision_object):
        gates = dict(decision_object["gates"])
        gates.pop("open_spine_row_worklist")
        return {
            "gates": gates,
            "preflight": decision_object["preflight"],
            "directives": decision_object["directives"],
            "judgment_points": decision_object["judgment_points"],
            "narration": decision_object["narration"],
            "next_move": decision_object["next_move"],
        }

    assert _strip(firing) == _strip(silent)
    jp_ids = {jp["id"] for jp in firing["judgment_points"]}
    assert not any("spine" in jp_id for jp_id in jp_ids)


def test_open_spine_row_gate_free_value_keys_appear_in_decisions_template(monkeypatch, tmp_path):
    """AC6: `directives_spine_worklist.FREE_VALUE_KEYS` is unioned into
    `preflight.decisions_template` by `__init__.py`, not hand-copied --
    proven the same way the sibling submodule union is proven above."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    template = decision_object["preflight"]["decisions_template"]
    assert set(_dc_spine_worklist.FREE_VALUE_KEYS).issubset(template.keys())
    for key in _dc_spine_worklist.FREE_VALUE_KEYS:
        assert template[key] is None
