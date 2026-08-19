"""
coordinator_core.workstream_complete.test_workstream_complete — conformance
suite for the `workstream-complete-assemble` computed-skill engine.

Scope (per dispatch brief, chunk W2-B1): the assembler emits a schema-valid
8-key envelope through `emit()`; `directives[]` name real, on-disk CLIs
(never invoked in-process); `judgment_points[]` are built via the shared
constructors, and the untrusted-gate ones carry no `recommendation`.

Run scoped only: `python -m pytest coordinator_core/workstream_complete/test_workstream_complete.py -q`
Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md, chunk W2-B1 [DEAD-CITATION: plan file never committed to this repo]
"""

from __future__ import annotations

import functools
import importlib.machinery
import importlib.util
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.ceremony_common import apply_halt
from coordinator_core.contract.decision_object.envelope import ENVELOPE_KEYS

# Real git spawn is load-bearing: terminal-status coverage tests read the
# DoE-claude repo's real HEAD `plan.schema.json` via `git show` to pin the
# schema enum against the actual on-disk oracle, and the no-commit-row guard
# builds real per-test commit history — no mock stands in for either.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]
from coordinator_core.session import harness_registry as hr
from coordinator_core.testing.doe_root import resolve_doe_root
import coordinator_core.workstream_complete as wsc
from coordinator_core.workstream_complete import apply as wsc_apply
from coordinator_core.workstream_complete import completion_verdict as _cv
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
        # `best_effort` is optional per a40dd5076 (a best-effort directive
        # cannot fail a ceremony); `advisory` is optional per
        # docs/plans/2026-08-15-coverage-gate-advisory-failure-and-warn-flood.md
        # chunk C2 (an advisory directive's failure never takes the run to
        # APPLY_EXIT_PARTIAL_MUTATION); `_gate_memo_key_parts` is optional
        # build-time metadata `_apply_write_trail_gate_memo` stamps and
        # `directives_review.py::record_gate_verdict_if_passed` reads
        # in-process to record the gate verdict under the same
        # `(session_id, sha_range)` identity the gate just checked -- it is
        # underscore-prefixed, never dispatched to a CLI, never part of the
        # wire shape a directive-consuming caller sees. The required set is
        # what this guard pins; an optional key landing later must not
        # silently widen it.
        assert set(directive.keys()) - {"best_effort", "advisory", "_gate_memo_key_parts"} == {
            "id",
            "cli",
            "args",
            "depends_on",
            "already_satisfied",
        }
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


#: Every directive id that constitutes a brightline-class gate, in EITHER
#: scope. The pin below asserts membership in this set rather than one
#: specific id, so a future re-scoping of the chain gate keeps the invariant
#: (SOME brightline gate fires) without needing this test rewritten — the
#: only edit that may legitimately shrink it is one that deletes a gate.
_BRIGHTLINE_DIRECTIVE_IDS = frozenset(
    {"d-run-review-brightline-gate", "d-run-chain-plan-brightline-gate"}
)


def test_every_disposition_computes_some_brightline_gate_directive(monkeypatch, tmp_path):
    """The invariant the 2026-08-03 doe-claude-em memo found violated: a
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


def test_chain_terminal_takes_the_session_scoped_gate_after_k006(monkeypatch, tmp_path):
    """A chain terminal now takes the SESSION-scoped brightline directive.

    Inverts this test's own prior assertion deliberately: the chain+plan
    two-oracle gate it used to require is removed (state/kill-ledger.md
    K-007, 2026-08-19, PM ruling — measured 7.4s per chain-terminal close
    to produce a review-scale verdict). The fallback is the session gate,
    never silence: a chain terminal with strictly less brightline gating
    than an ordinary session is the failure mode the sibling test below
    already guards, and it applies here for the same reason."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md"))
    directives = wsc.brief(decisions={}, repo_root=tmp_path)["directives"]
    ids = {d["id"] for d in directives}
    assert "d-run-chain-plan-brightline-gate" not in ids
    assert "d-run-review-brightline-gate" in ids


def test_no_directive_invokes_the_removed_brightline_gate_subcommand(monkeypatch, tmp_path):
    """Negative-spec for K-007: nothing may emit a directive that shells out
    to `wsc-coverage-gate-runner brightline-gate` — that subcommand no
    longer exists, so such a directive would fail at argv parsing."""
    for disposition, handoff in (("chain-terminal", "state/handoffs/x.md"), ("single-session", "")):
        _patch_gate(monkeypatch, _gate(disposition, consumed_handoff=handoff))
        for directive in wsc.brief(decisions={}, repo_root=tmp_path)["directives"]:
            assert "brightline-gate" not in (directive.get("args") or [])


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


def test_write_trail_directive_requires_all_five_review_fields(monkeypatch, tmp_path):
    """staff-eng review 2026-08-14 (F2): a truthy-but-incomplete `review` used
    to silently drop `d-write-trail` from the directive list while
    `build_wsc_tail_directive` still composed `--adjudication-present` for
    the SAME input -- the seam this raise now closes at assemble time,
    before `directives[]` is even built. See `_raise_on_review_truthy_
    unqualified`."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    partial_decisions = {"review": {"sha_range": "a..b", "reviewer": "someone"}}
    with pytest.raises(ValueError, match="qualifies for no"):
        wsc.brief(decisions=partial_decisions, repo_root=tmp_path)


# ---------------------------------------------------------------------------
# decisions["review"] as a list — a partitioned review's N per-slice records
# (each with its own sha_range/reviewer/scope/verdict/diff_loc) that the
# pre-existing single-object shape could not express. See
# `build_write_trail_directives`'s own docstring.
# ---------------------------------------------------------------------------

_REVIEW_SLICE_A = {
    "sha_range": "a1..a2",
    "reviewer": "code-reviewer",
    "scope": "chain",
    "verdict": "blocked",
    "diff_loc": 40,
}
_REVIEW_SLICE_B = {
    "sha_range": "b1..b2",
    "reviewer": "staff-eng",
    "scope": "chain",
    "verdict": "ok",
    "diff_loc": 60,
}


def test_write_trail_directive_accepts_a_list_of_per_slice_records(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {"review": [_REVIEW_SLICE_A, _REVIEW_SLICE_B]}
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    directives_by_id = {d["id"]: d for d in decision_object["directives"]}
    assert "d-write-trail-0" in directives_by_id
    assert "d-write-trail-1" in directives_by_id
    assert "d-write-trail" not in directives_by_id
    assert directives_by_id["d-write-trail-0"]["args"] == [
        "write-trail",
        "--sha-range", "a1..a2",
        "--reviewer", "code-reviewer",
        "--scope", "chain",
        "--verdict", "blocked",
        "--diff-loc", "40",
    ]
    assert directives_by_id["d-write-trail-1"]["args"] == [
        "write-trail",
        "--sha-range", "b1..b2",
        "--reviewer", "staff-eng",
        "--scope", "chain",
        "--verdict", "ok",
        "--diff-loc", "60",
    ]


def test_write_trail_directive_single_object_shape_is_byte_identical_to_today(monkeypatch, tmp_path):
    """Backward compatibility: a plain `dict` still produces exactly the
    one pre-existing `d-write-trail` id, never `d-write-trail-0`."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {"review": dict(_REVIEW_SLICE_A)}
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-write-trail" in ids
    assert "d-write-trail-0" not in ids


def test_write_trail_directive_list_drops_incomplete_entries_without_dropping_siblings(monkeypatch, tmp_path):
    incomplete = {"sha_range": "c1..c2", "reviewer": "code-reviewer"}  # missing scope/verdict/diff_loc
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {"review": [_REVIEW_SLICE_A, incomplete, _REVIEW_SLICE_B]}
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    # Index preserved from the ORIGINAL list position -- the incomplete
    # entry at index 1 contributes no directive, but index 2 (slice B)
    # keeps its own original index rather than shifting down to 1.
    assert "d-write-trail-0" in ids
    assert "d-write-trail-1" not in ids
    assert "d-write-trail-2" in ids


def test_write_trail_directive_empty_list_and_none_emit_nothing(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    for review_value in ([], None):
        decision_object = wsc.brief(decisions={"review": review_value}, repo_root=tmp_path)
        ids = {d["id"] for d in decision_object["directives"]}
        assert not any(i == "d-write-trail" or i.startswith("d-write-trail-") for i in ids)


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


def test_untrusted_gate_reported_point_stays_a_judgment_point_not_narration(monkeypatch, tmp_path):
    """Anti-scope: an untrusted-gate point (`recommendation=None`, e.g.
    `jp-session-shape`) must never be demoted into narration even when
    `partition_reportable` would mechanically classify it `reported` --
    only a RECOMMENDATION-carrying point is in this plan's class. Forces
    the uncertain-session-shape branch (an untrusted-gate point whose
    dispositions all resolve `d-coverage-gate`, absent here because this
    gate is single-session) and asserts it stays in `judgment_points[]`."""
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
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-session-shape" in jp_ids


def test_resolver_backed_review_partition_strategy_never_demoted_by_a_single_empty_call(monkeypatch, tmp_path):
    """Anti-scope: `review-partition-strategy` is one of the five resolver-
    backed points that build `resolves` through a resolver returning real
    directive ids once `decisions["review_partition"]` is populated, and
    `[]` only on an empty slice. A single call with no `review_partition`
    supplied must NOT demote it into `narration` -- that would reintroduce
    the over-count this plan's Problem section documents."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(
        decisions={
            "review": {
                "sha_range": "a..b",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "verdict": "ok",
                "diff_loc": 10,
            },
            "scratch_candidates": ["state/scratch/some-file.md"],
        },
        repo_root=tmp_path,
    )
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "review-partition-strategy" in jp_ids


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

_CHAIN_END_ROW_IDS = frozenset({5})


def _review_scale_decisions(**overrides: Any) -> dict:
    """Row-5/6 fixture base. `chain_partition_verdict` is GONE from this
    dict (state/kill-ledger.md K-007, 2026-08-19): the chain-scoped gate
    that produced it is removed, so a chain-terminal close resolves on the
    session-scoped brightline alone and row 6 is unreachable."""
    base: dict[str, Any] = dict(
        gross_loc=10,
        code_loc=10,
        commit_count=1,
        surface_count=1,
        executor_dispatched=False,
        shared_schema_touched=False,
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


# ---------------------------------------------------------------------------
# `gates.review_scale.commit_slices` / `.uncommitted_code_loc` -- end-to-end
# through `brief()` (A, docs/plans/2026-08-08-the-engine-asks-for-facts-it-
# already-holds.md C-followup). Direct producer coverage
# (`_measure_session_review_scale_inputs`'s `commit_slices_out` side
# channel, per-commit ordering/shape, the interleaved-foreign-commit case)
# lives in test_directives_review_scale.py; this section pins only the
# envelope-emission contract `brief()` itself owns: presence/absence of the
# key, and `uncommitted_code_loc`'s arithmetic.
# ---------------------------------------------------------------------------

_SESSION_ID_FOR_SLICES = "testsid123"  # must match `_gate`'s fixed `sid`

_SLICE_TEST_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git_slice(args: list[str], cwd: Path, **kwargs: Any):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, **_SLICE_TEST_NO_CONSOLE, **kwargs
    )


def _init_repo_with_session_commit(root: Path) -> str:
    _git_slice(["init", "-q"], root)
    _git_slice(["config", "user.email", "t@example.com"], root)
    _git_slice(["config", "user.name", "t"], root)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git_slice(["add", "a.py"], root)
    _git_slice(["commit", "-q", "-m", "init"], root)

    (root / "b.py").write_text("y = 1\ny2 = 2\n", encoding="utf-8")
    _git_slice(["add", "b.py"], root)
    _git_slice(["commit", "-q", "-m", f"session work\n\nSession-Id: {_SESSION_ID_FOR_SLICES}"], root)
    out = _git_slice(["rev-parse", "HEAD"], root, text=True)
    return out.stdout.strip()


def test_brief_emits_commit_slices_and_uncommitted_code_loc_for_a_real_commit(monkeypatch, tmp_path):
    """(4) `uncommitted_code_loc` equals measured `code_loc` minus the summed
    slice `diff_loc`; a session with exactly one owned commit and no dirty
    files gets a one-entry slice list and `uncommitted_code_loc == 0`."""
    sha = _init_repo_with_session_commit(tmp_path)
    gate = wsc.SessionShapeGate(
        sid=_SESSION_ID_FOR_SLICES,
        disposition="single-session",
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )
    _patch_gate(monkeypatch, gate)

    decision_object = wsc.brief(decisions={"stage_paths": []}, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]

    assert "commit_slices" in review_scale
    slices = review_scale["commit_slices"]
    assert len(slices) == 1
    assert slices[0]["sha"] == sha
    assert slices[0]["sha_range"] == f"{sha}^..{sha}"
    assert slices[0]["diff_loc"] == 2
    assert slices[0]["scope_kind"] == "diff"

    assert review_scale["uncommitted_code_loc"] == 0


def test_brief_emits_uncommitted_code_loc_with_zero_commits_and_dirty_files(monkeypatch, tmp_path):
    """(4) The `commit_slices == []` / non-zero `uncommitted_code_loc` case:
    a session with no owned commits and uncommitted work gets an EMPTY (but
    present) slice list, and `uncommitted_code_loc` equal to the whole
    measured `code_loc` (nothing sliced off it)."""
    _git_slice(["init", "-q"], tmp_path)
    _git_slice(["config", "user.email", "t@example.com"], tmp_path)
    _git_slice(["config", "user.name", "t"], tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git_slice(["add", "a.py"], tmp_path)
    _git_slice(["commit", "-q", "-m", "init"], tmp_path)

    (tmp_path / "dirty.py").write_text("z = 1\nz2 = 2\nz3 = 3\n", encoding="utf-8")

    gate = wsc.SessionShapeGate(
        sid=_SESSION_ID_FOR_SLICES,
        disposition="single-session",
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )
    _patch_gate(monkeypatch, gate)

    decision_object = wsc.brief(decisions={"stage_paths": ["dirty.py"]}, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]

    assert "commit_slices" in review_scale
    assert review_scale["commit_slices"] == []
    assert review_scale["uncommitted_code_loc"] == 3


def test_brief_omits_commit_slices_key_when_measurement_unresolvable(monkeypatch, tmp_path):
    """(3) The unresolvable case OMITS the key entirely -- never present-
    but-empty. `tmp_path` here is deliberately NOT a git repository, so
    `_session_owned_shas`'s `git log` spawn fails and the whole four-tuple
    resolves to `None`."""
    gate = wsc.SessionShapeGate(
        sid=_SESSION_ID_FOR_SLICES,
        disposition="single-session",
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )
    _patch_gate(monkeypatch, gate)

    decision_object = wsc.brief(decisions={"stage_paths": []}, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]

    assert "commit_slices" not in review_scale
    assert "uncommitted_code_loc" not in review_scale


def test_review_scale_judgment_point_resolved_non_trivial_row_keeps_acknowledge_scale(monkeypatch, tmp_path):
    """(f) the RESOLVED branch is untouched by the mechanism-3 fix: a
    resolved, non-1/2 row (here row 5, the base `_review_scale_decisions()`
    fixture's own resolution -- already non-trivial, left as-is) still
    carries the trusted `acknowledge-scale` recommendation via
    `build_judgment_point`.

    Surface moved (`docs/plans/2026-08-15-judgment-points-that-gate-
    nothing-stop-being-questions.md` C2): the resolved branch always sets
    `reportable=True` with `resolves=[]`, so `contract/decision_object/
    judgment.py::partition_reportable` now demotes it out of
    `decision_object["judgment_points"]` -- `narration` (the only surface
    `_narration_and_next_move` folds a reported point's id/question/
    rationale into; the C2 plan deliberately added no new envelope key)
    is what this test asserts against instead. Checked on the specific
    disposition and rationale text, not a bare `"jp-review-scale"`
    substring, so a narration change unrelated to this recommendation
    still fails this test rather than passing on any narration edit."""
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions=_review_scale_decisions(), repo_root=tmp_path)
    ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-scale" not in ids
    narration = decision_object["narration"]
    assert "jp-review-scale (" in narration
    assert (
        "-- review scale row 5 (code-reviewer): chain-terminal with the "
        "session-scoped brightline resolved and not tripped" in narration
    )


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


# ---------------------------------------------------------------------------
# `gates.review_scale.chain_slices` (Seam 3, C2 -> C3, this plan) -- the
# chain-scoped review-obligation slate, read back onto the SAME record
# `chain_partition_verdict` above already reads. Present / resolved-empty /
# key-absent-when-unresolvable, mirroring the `commit_slices` trio above,
# plus the negative-spec that `chain_slices` and `commit_slices` are
# different sets that must both be able to appear independently.
# ---------------------------------------------------------------------------

_CHAIN_SLICE_ENTRY = {
    "sha": "abc1234",
    "sha_range": "abc1234^..abc1234",
    "recordable": True,
    "certifies_review": False,
}


# ---------------------------------------------------------------------------
# AC9 -- `_build_write_trail_args` forwards `reviewer_evidence` as
# `--reviewer-evidence`, mirroring the existing `--scope-kind` handling. A
# review WITHOUT evidence stays byte-identical to today.
# ---------------------------------------------------------------------------


def test_build_write_trail_args_forwards_reviewer_evidence_when_present():
    review = {
        "sha_range": "a..b",
        "reviewer": "coordinator:code-reviewer",
        "scope": "diff",
        "verdict": "OK",
        "diff_loc": 10,
        "reviewer_evidence": "state/subagent-share/sid/reviewer.md",
    }
    args = wsc._build_write_trail_args(review)
    assert args == [
        "write-trail",
        "--sha-range", "a..b",
        "--reviewer", "coordinator:code-reviewer",
        "--scope", "diff",
        "--verdict", "OK",
        "--diff-loc", "10",
        "--reviewer-evidence", "state/subagent-share/sid/reviewer.md",
    ]


def test_build_write_trail_args_is_byte_identical_without_reviewer_evidence():
    review = {
        "sha_range": "a..b",
        "reviewer": "coordinator:code-reviewer",
        "scope": "diff",
        "verdict": "OK",
        "diff_loc": 10,
    }
    args = wsc._build_write_trail_args(review)
    assert args == [
        "write-trail",
        "--sha-range", "a..b",
        "--reviewer", "coordinator:code-reviewer",
        "--scope", "diff",
        "--verdict", "OK",
        "--diff-loc", "10",
    ]
    assert "--reviewer-evidence" not in args


def test_build_write_trail_directives_names_reviewer_evidence_on_the_directive():
    """End-to-end through `build_write_trail_directives`: a `decisions
    ["review"]` dict carrying `reviewer_evidence` produces a `d-write-trail`
    directive whose args name `--reviewer-evidence`."""
    review = {
        "sha_range": "a..b",
        "reviewer": "coordinator:code-reviewer",
        "scope": "diff",
        "verdict": "OK",
        "diff_loc": 10,
        "reviewer_evidence": "state/subagent-share/sid/reviewer.md",
    }
    directives = wsc.build_write_trail_directives(review)
    assert len(directives) == 1
    args = directives[0]["args"]
    assert "--reviewer-evidence" in args
    assert args[args.index("--reviewer-evidence") + 1] == "state/subagent-share/sid/reviewer.md"


# ---------------------------------------------------------------------------
# d-archive-session-claim removed from the assembly (2026-07-28) -- this
# ceremony fires once per closed workstream, but session-dir archival
# (`scope.archive()`) is a once-per-SESSION-END operation. Emitting the
# archive directive here archived a still-live session mid-session,
# destroying once-per-session sentinels and the dispatch-evidence file.
# Archival is now wired to session END (a SessionEnd hook, DoE-claude repo),
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

    jp = next(jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-commit-subject-missing")
    assert "decisions['subject']" in jp["reason"]
    assert "no disposition" in jp["reason"]


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
# Leg A, kind: session-handoff — cross-repo/inbox/2026-08-03-doe-claude-em-
# wsc-leg-a-session-handoff-kind-blind.md: that kind never carries its own
# `## Acceptance criteria` (0/34 in DoE-claude's corpus, 0/22 in claude-klabauter's),
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
    close_out_last_partial: str | None = None,
) -> None:
    plan_path = tmp_path / rel_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    marker_line = (
        f"close_out_last_partial: {close_out_last_partial}\n" if close_out_last_partial is not None else ""
    )
    plan_path.write_text(
        f"---\nstatus: {status}\ndeliverable_id: {deliverable_id}\n{marker_line}---\n\n{body}\n",
        encoding="utf-8",
    )


@functools.lru_cache()
def _leg_a_non_terminal_schema_statuses() -> list[str] | None:
    """Mirrors `test_leg_a_terminal_plan_status_covers_every_terminal_member_
    of_the_schema_enum`'s own schema-fetch mechanism (DoE HEAD `git show`,
    `None` on an unregistered/missing DoE repo -- the caller turns that into
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
    # hard failure on a git-show error against a *present* DoE checkout;
    # only "no DoE repo" collapses to None/skip, not a broken checkout.
    assert result.returncode == 0, f"Cannot read DoE HEAD plan.schema.json: {result.stderr.strip()}"
    doe_plan_schema = json.loads(result.stdout)
    schema_enum = set(doe_plan_schema["properties"]["status"]["enum"])
    return sorted(schema_enum - wsc._LEG_A_TERMINAL_PLAN_STATUS)


def pytest_generate_tests(metafunc):
    """Lazy parametrize source for `status` below — deferred to pytest's own
    collection-generation hook (a function body, not a module-level
    statement) rather than a module-level constant, so the `git show`
    subprocess in `_leg_a_non_terminal_schema_statuses` fires only when
    pytest actually generates this test's cases, never merely on import."""
    if "status" not in metafunc.fixturenames:
        return
    if metafunc.function is not test_session_handoff_leg_a_open_when_joined_plan_status_not_terminal:
        return
    statuses = _leg_a_non_terminal_schema_statuses()
    metafunc.parametrize(
        "status",
        statuses
        or [pytest.param("draft", marks=pytest.mark.skip(reason="DoE-claude repo not registered/found on this machine"))],
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


def test_session_handoff_leg_a_indeterminate_when_terminal_plan_still_carries_close_out_marker(
    monkeypatch, tmp_path
):
    """C4/AC5-AC6: a terminal-status joined plan that STILL carries
    `close_out_last_partial:` cannot be trusted the way an ordinary
    terminal plan can -- the marker itself records that the last close-out
    attempt found the plan not fully shipped. This must resolve
    `indeterminate` (non-blocking, but reported), not `not-applicable`
    (silent)."""
    _write_session_handoff(tmp_path, "state/handoffs/x.md", "dlv-thing")
    _write_session_handoff_plan(
        tmp_path,
        "docs/plans/2026-08-03-thing.md",
        "no AC heading at all\n",
        status="implemented",
        close_out_last_partial="2026-08-06T14:31:36Z -- 1 missing (joined): C5",
    )
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "indeterminate"
    assert "docs/plans/2026-08-03-thing.md" in leg_a["detail"]
    assert "'implemented'" in leg_a["detail"]
    assert "close_out_last_partial" in leg_a["detail"]
    assert "2026-08-06T14:31:36Z -- 1 missing (joined): C5" in leg_a["detail"]
    # indeterminate is non-blocking (AC8): no new blocking condition.
    assert gate_evidence["elements"][0]["blocks"] is False
    assert gate_evidence["blocks"] is False


def test_session_handoff_leg_a_not_applicable_when_terminal_plan_has_no_close_out_marker(
    monkeypatch, tmp_path
):
    """C4's noise-suppression regression test -- the single most important
    case in this chunk: the ordinary 30-of-34 terminal-and-clean case must
    keep returning `not-applicable`, SILENTLY -- it must not appear in the
    gate's `indeterminate_notes` summary tail."""
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
    leg_a = gate_evidence["elements"][0]["leg_a"]
    assert leg_a["verdict"] == "not-applicable"
    assert leg_a["detail"] == "plan docs/plans/2026-08-03-thing.md: status 'implemented' is terminal"
    assert "indeterminate:" not in gate_evidence["summary_line"]
    assert gate_evidence["summary_line"] == "Consumed-handoff completeness: all consumed handoffs clear"


def test_consumed_handoff_completeness_summary_line_names_plan_for_uncleared_marker(monkeypatch, tmp_path):
    """AC6, gate-level: `summary_line` must stop reading 'all consumed
    handoffs clear' and instead name the plan and the reason -- traced end
    to end through `compute_consumed_handoff_completeness_gate`'s own
    `indeterminate_notes` wiring, not merely inferred from leg_a's verdict."""
    _write_session_handoff(tmp_path, "state/handoffs/x.md", "dlv-thing")
    _write_session_handoff_plan(
        tmp_path,
        "docs/plans/2026-08-03-thing.md",
        "no AC heading at all\n",
        status="implemented",
        close_out_last_partial="2026-08-06T14:31:36Z -- 1 missing (joined): C5",
    )
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=("state/handoffs/x.md",)))
    _patch_leg_b(monkeypatch, {"exit_code": 1, "referenced": False})

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)

    gate_evidence = decision_object["gates"]["consumed_handoff_completeness"]
    assert gate_evidence["summary_line"] != "Consumed-handoff completeness: all consumed handoffs clear"
    assert "docs/plans/2026-08-03-thing.md" in gate_evidence["summary_line"]
    assert "indeterminate:" in gate_evidence["summary_line"]
    # Still non-blocking end to end (AC8).
    assert gate_evidence["blocks"] is False


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
        pytest.skip("DoE-claude repo not registered on this machine")
    doe_repo = Path(doe_root)
    if not doe_repo.exists():
        pytest.skip(f"DoE repo not found at {doe_repo}")

    result = subprocess.run(
        ["git", "-C", str(doe_repo), "show", "HEAD:coordinator/schemas/plan.schema.json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, f"Cannot read DoE HEAD plan.schema.json: {result.stderr.strip()}"
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
    # (C2 redo, docs/plans/2026-08-15-judgment-points-that-gate-nothing-
    # stop-being-questions.md) `wsc.brief()`'s returned `judgment_points[]`
    # is now POST-`partition_reportable` (asked-only) -- a `reportable=True`
    # point (e.g. `cross-cutting-check`) never appears there once demoted
    # into `narration`, even though `judgments.py` still builds it. This
    # sweep's OWN purpose (every id `judgments.py` can build, every
    # directive id, every `resolves` id) needs the PRE-partition set, so a
    # spy on `partition_reportable` captures its raw input before
    # delegating through unchanged. A demoted point's own `resolves` is
    # always `[]` by construction (that is what "gate-nothing" means), so
    # folding it into `judgment_point_ids` cannot introduce a phantom
    # `resolves` id into the sibling phantom-directive-id test below.
    from coordinator_core.contract.decision_object.judgment import (
        partition_reportable as _real_partition_reportable,
    )

    def _spy_partition_reportable(judgment_points, directives):
        judgment_point_ids.update(jp["id"] for jp in judgment_points)
        return _real_partition_reportable(judgment_points, directives)

    monkeypatch.setattr(wsc, "partition_reportable", _spy_partition_reportable)
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
    all-prefix check, but the pre-existing rule for a `scope_size` of 2 or
    more still
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
    and a `scope_size` of 2 or more must flag regardless of `matched_scope_entry_
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
        # `reportable` and `resolves_computed` are OPTIONAL keys
        # (docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-being-
        # questions.md): each is present only on a point that explicitly
        # authored it -- an action-class (`reportable=False`) point stays a
        # real `judgment_points[]` entry and carries the key, and a
        # resolver-backed point carries `resolves_computed=True`. Both are
        # subtracted here rather than asserted as fixed members, mirroring
        # how `advisory`/`best_effort` are handled on directives elsewhere.
        assert set(jp.keys()) - {"reportable", "resolves_computed"} == {
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
        "path": str(tmp_path / "docs" / "plans" / f"{slug}.md"),
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
        "path": str(tmp_path / "docs" / "plans" / f"{decisions_slug}.md"),
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
        "path": str(tmp_path / "docs" / "plans" / f"{decisions_slug}.md"),
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
        "path": None,
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
        "path": str(tmp_path / "docs" / "plans" / f"{slug}.md"),
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


def _commit_with_session_trailer(root: Path, name: str, sid: str) -> None:
    """Shared by AC13/AC14's interleaved-peer fixtures (review-integrator
    finding, P3, 2026-08-12) -- was a byte-identical nested `_commit` in
    both `test_measure_session_review_scale_inputs_commit_count_ignores_
    interleaved_peer_commits` and `test_review_trail_guard_foreign_flag_
    implies_excluded_from_commits_arm`, extracted per this module's own
    `_init_git_repo` precedent of a module-level fixture helper."""
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"add {name}", "--trailer", f"Session-Id: {sid}"],
        cwd=root,
        check=True,
    )


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


def test_classify_session_authored_files_with_none_start_time_issues_no_git_log_call(tmp_path, monkeypatch):
    """`session_start_time=None`: predicate (a) is not computable, so the
    batched `_session_created_paths` git-log call must not run at all."""
    from coordinator_core.workstream_complete import directives_memo_lifecycle

    _init_git_repo(tmp_path)
    (tmp_path / "some-untracked.md").write_text("content\n", encoding="utf-8")

    def _fail_if_called(repo_root, args):
        if args and args[0] == "log":
            raise AssertionError(f"git log must not be spawned when session_start_time is None, got {args}")
        return real_run_git(repo_root, args)

    real_run_git = directives_memo_lifecycle._run_git

    monkeypatch.setattr(directives_memo_lifecycle, "_run_git", _fail_if_called)

    results = directives_memo_lifecycle.classify_session_authored_files(
        tmp_path, session_start_time=None, known_concurrent_paths=frozenset()
    )
    assert results


def test_classify_session_authored_files_batches_one_git_log_call_regardless_of_dirty_count(tmp_path, monkeypatch):
    """Regression guard for the N+1 hang: with N dirty files, the number of
    `git log --diff-filter=A` calls must stay 1, not scale with N."""
    from datetime import datetime, timedelta, timezone

    from coordinator_core.workstream_complete import directives_memo_lifecycle

    _init_git_repo(tmp_path)
    for i in range(12):
        (tmp_path / f"dirty-{i}.md").write_text(f"content {i}\n", encoding="utf-8")

    session_start_time = datetime.now(timezone.utc) - timedelta(hours=1)

    real_run_git = directives_memo_lifecycle._run_git
    log_calls: list[list[str]] = []

    def _counting_run_git(repo_root, args):
        if args and args[0] == "log" and "--diff-filter=A" in args:
            log_calls.append(args)
        return real_run_git(repo_root, args)

    monkeypatch.setattr(directives_memo_lifecycle, "_run_git", _counting_run_git)

    results = directives_memo_lifecycle.classify_session_authored_files(
        tmp_path, session_start_time, known_concurrent_paths=frozenset()
    )
    assert len(results) == 12
    assert len(log_calls) == 1, (
        f"expected exactly one batched 'git log --diff-filter=A' call regardless of dirty-file "
        f"count, got {len(log_calls)}: {log_calls}"
    )
    assert "--" not in log_calls[0], "the batched call must carry no pathspec -- per-path spawning is the regression"


def test_classify_session_authored_files_git_failure_degrades_predicate_a_to_false(tmp_path, monkeypatch):
    """A failing/erroring batched git-log call must degrade predicate (a) to
    False for every path, never raise and never mark everything authored."""
    from datetime import datetime, timedelta, timezone

    import subprocess as subprocess_module

    from coordinator_core.workstream_complete import directives_memo_lifecycle

    _init_git_repo(tmp_path)
    (tmp_path / "tracked-dirty.md").write_text("committed\n", encoding="utf-8")
    subprocess_module.run(["git", "add", "tracked-dirty.md"], cwd=tmp_path, check=True)
    subprocess_module.run(["git", "commit", "-q", "-m", "add tracked-dirty"], cwd=tmp_path, check=True)

    session_start_time = datetime.now(timezone.utc) - timedelta(hours=1)
    # Dirty (but not untracked) the already-committed file so predicate (b)
    # -- which requires "??" untracked status -- cannot fire regardless of
    # the git-log failure this test is isolating predicate (a) against.
    (tmp_path / "tracked-dirty.md").write_text("edited\n", encoding="utf-8")

    real_run_git = directives_memo_lifecycle._run_git

    def _fail_only_log(repo_root, args):
        if args and args[0] == "log" and "--diff-filter=A" in args:
            return None
        return real_run_git(repo_root, args)

    monkeypatch.setattr(directives_memo_lifecycle, "_run_git", _fail_only_log)

    results = directives_memo_lifecycle.classify_session_authored_files(
        tmp_path, session_start_time, known_concurrent_paths=frozenset()
    )
    row = next(r for r in results if r["path"] == "tracked-dirty.md")
    assert row["session_authored"] is False
    assert row["reason"] == "fails predicate (a) and (b)"


def test_classify_session_authored_files_equivalent_to_per_path_predicate(tmp_path):
    """Equivalence: files added since session_start_time vs. before it must
    classify identically to the (removed) per-path `_created_this_session`
    predicate, including exact `reason` strings."""
    import subprocess as subprocess_module
    import time
    from datetime import datetime, timezone

    from coordinator_core.workstream_complete import directives_memo_lifecycle

    _init_git_repo(tmp_path)

    # Committed before session_start_time -- not session-authored via (a).
    (tmp_path / "old-file.md").write_text("old\n", encoding="utf-8")
    subprocess_module.run(["git", "add", "old-file.md"], cwd=tmp_path, check=True)
    subprocess_module.run(["git", "commit", "-q", "-m", "old"], cwd=tmp_path, check=True)

    # `--since` compares at whole-second granularity -- pad on both sides so
    # the old/new commits land unambiguously before/after session_start_time.
    time.sleep(1.5)
    session_start_time = datetime.now(timezone.utc)
    time.sleep(1.5)

    # Committed after session_start_time -- session-authored via (a).
    (tmp_path / "new-file.md").write_text("new\n", encoding="utf-8")
    subprocess_module.run(["git", "add", "new-file.md"], cwd=tmp_path, check=True)
    subprocess_module.run(["git", "commit", "-q", "-m", "new"], cwd=tmp_path, check=True)

    # Dirty the committed files so both appear in porcelain status.
    (tmp_path / "old-file.md").write_text("old edited\n", encoding="utf-8")
    (tmp_path / "new-file.md").write_text("new edited\n", encoding="utf-8")

    results = {
        r["path"]: r
        for r in directives_memo_lifecycle.classify_session_authored_files(
            tmp_path, session_start_time, known_concurrent_paths=frozenset()
        )
    }

    assert results["old-file.md"]["session_authored"] is False
    assert results["old-file.md"]["reason"] == "fails predicate (a) and (b)"
    assert results["new-file.md"]["session_authored"] is True
    assert results["new-file.md"]["reason"] == "predicate (a): created this session"


def test_classify_session_authored_files_batched_path_handles_quoted_filename(tmp_path):
    """A path needing git's quoting (non-ASCII) must classify correctly
    through the batched `--name-only` path -- exercises the normalization
    convention this module shares with `_git_status_porcelain`: neither
    call forces `core.quotepath=false`, so both see the SAME quoted-octal
    form for a non-ASCII path and the string-equality membership test
    still lines up (see this module's own docstring on why no override is
    applied). `git status --porcelain` is the source of truth for what key
    a caller sees; predicate (a) here proves the batched git-log path
    produces a matching key for the same file, not an unquoted one that
    would silently fail membership."""
    import subprocess as subprocess_module
    from datetime import datetime, timedelta, timezone

    from coordinator_core.workstream_complete import directives_memo_lifecycle

    _init_git_repo(tmp_path)

    weird_name = "café-notes.md"
    (tmp_path / weird_name).write_text("notes\n", encoding="utf-8")
    subprocess_module.run(["git", "add", weird_name], cwd=tmp_path, check=True)
    subprocess_module.run(["git", "commit", "-q", "-m", "add weird name"], cwd=tmp_path, check=True)

    session_start_time = datetime.now(timezone.utc) - timedelta(hours=1)

    # Dirty the committed file so it shows up in porcelain status too.
    (tmp_path / weird_name).write_text("notes edited\n", encoding="utf-8")

    results = directives_memo_lifecycle.classify_session_authored_files(
        tmp_path, session_start_time, known_concurrent_paths=frozenset()
    )
    assert len(results) == 1, f"expected exactly one dirty path, got {results}"
    row = results[0]
    assert weird_name in row["path"] or "caf" in row["path"], f"unexpected path key: {row['path']!r}"
    assert row["session_authored"] is True
    assert row["reason"] == "predicate (a): created this session"


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
    each `directives_*` submodule's own `FREE_VALUE_KEYS` constant (AC3).
    Every key stays `None` EXCEPT the three C4/AC5-declared keys
    (`wsc.DECISIONS_TEMPLATE_RESOLVED_KEY_ENVELOPE_PATHS`), which now
    pre-fill from data this SAME run already resolved rather than a
    hand-copied list or a phantom key absent from every submodule's
    constant — see `test_decisions_template_prefills_*` above for the
    positive assertions on those three."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}

    template = decision_object["preflight"]["decisions_template"]
    free_value_keys_in_template = set(template.keys()) - jp_ids
    assert free_value_keys_in_template == _expected_free_value_keys()
    for key in free_value_keys_in_template:
        if key in wsc.DECISIONS_TEMPLATE_RESOLVED_KEY_ENVELOPE_PATHS:
            continue
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
# 2026-07-30 doe-claude-em cross-repo memo (`cross-repo/archive/2026-07-30-
# doe-claude-em-wsc-review-trail-passthrough-and-memo-attribution.md`), item
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
# 2026-07-30 doe-claude-em cross-repo memo, item 2 -- an unrecognized flat
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
            "reviewer": "staff-eng",
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
# 2026-08-03 doe-claude-em-wsc-tail-review-metadata-dropped -- the transport
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
            "verdict": "ok",
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
# state/bug-backlog/2026-08-14-no-caller-composes-b-adjudication-present-
# 9f4c21a0e5d3.yaml -- build_wsc_tail_directive composes --adjudication-
# present whenever decisions["review"] is non-empty, the PM-ruled predicate
# that finally gives the b_adjudication_present fail-loud gate (7c19e190ae86)
# a real producer.
# ---------------------------------------------------------------------------


def test_build_wsc_tail_directive_composes_adjudication_present_for_nonempty_review():
    decisions = {
        "review": {
            "sha_range": "abc123..def456",
            "reviewer": "staff-eng",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 42,
        },
    }
    directive = _dc_tail.build_wsc_tail_directive("abcdef", decisions)
    assert "--adjudication-present" in directive["args"]
    assert directive["args"][-1] == "{d-close-tail-args.argv}"


def test_build_wsc_tail_directive_composes_adjudication_present_for_list_shaped_review():
    decisions = {"review": [_REVIEW_SLICE_A, _REVIEW_SLICE_B]}
    directive = _dc_tail.build_wsc_tail_directive("abcdef", decisions)
    assert "--adjudication-present" in directive["args"]
    assert directive["args"][-1] == "{d-close-tail-args.argv}"


# ---------------------------------------------------------------------------
# staff-eng review 2026-08-14 (coordinatorstaff-eng-659e50f5) F1/F2 --
# a truthy-but-incomplete decisions["review"] used to compose
# --adjudication-present in build_wsc_tail_directive while
# build_close_tail_args_directive composed NO --review-*/--review-slice
# token at all -- the seam surfaced only post-commit, as an exit_code 2 the
# operator is told not to re-run for. `_review_any_qualifies` now backs
# BOTH builders' predicate, and a truthy-but-unqualified `review` raises
# ValueError at assemble time from EITHER builder instead of the pair
# silently disagreeing.
# ---------------------------------------------------------------------------


def test_truthy_incomplete_dict_review_raises_from_both_builders_instead_of_pairing_mismatch():
    decisions = {"review": {"sha_range": "abc..def", "reviewer": "staff-eng"}}  # missing 3 fields
    with pytest.raises(ValueError, match="qualifies for no"):
        _dc_tail.build_wsc_tail_directive("abcdef", decisions)
    with pytest.raises(ValueError, match="qualifies for no"):
        _dc_tail.build_close_tail_args_directive(decisions)


def test_truthy_all_entries_incomplete_list_review_raises_from_both_builders():
    decisions = {"review": [{"sha_range": "a..b"}, {"reviewer": "x"}]}
    with pytest.raises(ValueError, match="qualifies for no"):
        _dc_tail.build_wsc_tail_directive("abcdef", decisions)
    with pytest.raises(ValueError, match="qualifies for no"):
        _dc_tail.build_close_tail_args_directive(decisions)


def test_build_wsc_tail_directive_omits_adjudication_present_for_falsy_or_absent_review():
    baseline = _dc_tail.build_wsc_tail_directive("abcdef", {"governing_plan_slug": "some-plan"})
    assert "--adjudication-present" not in baseline["args"]
    for falsy_review in (None, {}, [], False, ""):
        directive = _dc_tail.build_wsc_tail_directive(
            "abcdef", {"governing_plan_slug": "some-plan", "review": falsy_review}
        )
        assert directive["args"] == baseline["args"], (
            "a falsy/absent decisions['review'] must produce argv byte-identical "
            "to the no-review-key baseline"
        )


# ---------------------------------------------------------------------------
# List-shaped decisions["review"] reaching the commit-tail path (finishes the
# partitioned-review fix's second half -- directives_review.py's
# build_write_trail_directives covered the standalone directive path in
# a3d90f24dc16; this is the wsc-close tail-args -> wsc-tail.py leg).
# ---------------------------------------------------------------------------


def test_build_close_tail_args_directive_single_dict_review_byte_identical():
    """Backward compatibility is absolute: a single-`dict` review must keep
    producing the exact same scalar `--review-*` flags it always has, never
    the new `--review-slice` form."""
    decisions = {
        "review": {
            "sha_range": "abc123..def456",
            "reviewer": "staff-eng",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 42,
        },
    }
    directive = _dc_tail.build_close_tail_args_directive(decisions)
    assert directive["args"] == [
        "tail-args",
        "--review-sha-range", "abc123..def456",
        "--review-reviewer", "staff-eng",
        "--review-scope", "chain",
        "--review-verdict", "ok",
        "--review-diff-loc", "42",
    ]
    assert "--review-slice" not in directive["args"]


def test_build_close_tail_args_directive_list_review_emits_one_slice_per_qualifying_entry():
    import json as _json

    decisions = {
        "review": [
            {
                "sha_range": "a1..a2",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "verdict": "ok",
                "diff_loc": 10,
            },
            {"sha_range": "incomplete-entry-dropped"},  # missing required fields -- no token
            {
                "sha_range": "b1..b2",
                "reviewer": "staff-eng",
                "scope": "session",
                "verdict": "warn",
                "diff_loc": 20,
                "scope_kind": "diff",
            },
        ],
    }
    directive = _dc_tail.build_close_tail_args_directive(decisions)
    args = directive["args"]
    assert "--review-sha-range" not in args
    slice_indices = [i for i, tok in enumerate(args) if tok == "--review-slice"]
    assert len(slice_indices) == 2
    payloads = [_json.loads(args[i + 1]) for i in slice_indices]
    assert payloads[0]["sha_range"] == "a1..a2"
    assert payloads[1]["sha_range"] == "b1..b2"
    assert payloads[1]["scope_kind"] == "diff"
    assert "scope_kind" not in payloads[0]


def test_build_close_tail_args_directive_forwards_reviewer_evidence_scalar():
    """Regression (DoE-claude memo 2026-08-13 § 2): `reviewer_evidence` was
    accepted in `decisions["review"]`, never emitted here, and the op-side gate
    (`review_trail_write._verify_reviewer_evidence`) then warned that the field
    was missing — the correlation it checks was severed one layer above it."""
    decisions = {
        "review": {
            "sha_range": "abc123..def456",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 42,
            "reviewer_evidence": "state/subagent-share/sid/reviewer.md",
        },
    }
    args = _dc_tail.build_close_tail_args_directive(decisions)["args"]
    assert args[-2:] == [
        "--review-reviewer-evidence",
        "state/subagent-share/sid/reviewer.md",
    ]


def test_build_close_tail_args_directive_forwards_reviewer_evidence_per_slice():
    import json as _json

    decisions = {
        "review": [
            {
                "sha_range": "a1..a2",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "verdict": "ok",
                "diff_loc": 10,
                "reviewer_evidence": "state/subagent-share/sid/a.md",
            },
            {
                "sha_range": "b1..b2",
                "reviewer": "staff-eng",
                "scope": "session",
                "verdict": "warn",
                "diff_loc": 20,
            },
        ],
    }
    args = _dc_tail.build_close_tail_args_directive(decisions)["args"]
    payloads = [
        _json.loads(args[i + 1]) for i, tok in enumerate(args) if tok == "--review-slice"
    ]
    assert payloads[0]["reviewer_evidence"] == "state/subagent-share/sid/a.md"
    assert "reviewer_evidence" not in payloads[1]


# ---------------------------------------------------------------------------
# 2026-08-14 state/bug-backlog/2026-08-14-wsc-apply-accepts-an-unconsumed-
# decision-debea052f8c5.yaml -- decisions["review"] nested one key deeper
# than either accepted shape (e.g. {"slices": [...], ...}) passed both
# reader sites silently and the ceremony still exited 0. The shared
# `validate_review_shape` now raises loud, from both sites, on the same
# malformed payload.
# ---------------------------------------------------------------------------

_UNCONSUMED_REVIEW_PAYLOAD = {
    "slices": [
        {
            "sha_range": "a1..a2",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        },
    ],
    "reviewer": "someone",
    "scope": "chain",
    "verdict": "ok",
    "reviewer_evidence": "state/subagent-share/sid/reviewer.md",
}


def test_build_close_tail_args_directive_raises_on_unconsumed_review_dict_shape():
    """The exact failing payload from cross-repo/inbox/2026-08-14-project-
    cockpit-em-review-decisions-dict-drops-silently-and-mints-a-not-reviewed-
    waiver.md — a 'slices' wrapper key that is not part of either accepted
    shape must now raise, naming both legal shapes, instead of silently
    contributing zero --review-* tokens."""
    decisions = {"review": _UNCONSUMED_REVIEW_PAYLOAD}
    with pytest.raises(ValueError) as excinfo:
        _dc_tail.build_close_tail_args_directive(decisions)
    message = str(excinfo.value)
    assert "subset of" in message, "must name the dict-of-recognized-keys legal shape"
    assert "list of such dicts" in message, "must name the list legal shape"
    assert "'slices'" in message


def test_build_write_trail_directives_raises_on_the_same_unconsumed_review_dict_shape():
    """The second independent dropping site named in the backlog entry —
    `build_write_trail_directives` must call the SAME validator, so it
    cannot diverge from `build_close_tail_args_directive`'s verdict on the
    identical payload."""
    with pytest.raises(ValueError) as excinfo:
        wsc.build_write_trail_directives(_UNCONSUMED_REVIEW_PAYLOAD)
    assert "'slices'" in str(excinfo.value)


def test_validate_review_shape_accepts_legal_single_dict_and_still_emits_five_flags():
    """A legal single dict (byte-identical shape to
    `test_build_close_tail_args_directive_single_dict_review_byte_identical`)
    must still pass validation and still emit the exact five scalar
    `--review-*` flags it does today — the new validator changes what is
    REJECTED, never what a legal payload produces."""
    decisions = {
        "review": {
            "sha_range": "abc123..def456",
            "reviewer": "staff-eng",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 42,
        },
    }
    directive = _dc_tail.build_close_tail_args_directive(decisions)
    assert directive["args"] == [
        "tail-args",
        "--review-sha-range", "abc123..def456",
        "--review-reviewer", "staff-eng",
        "--review-scope", "chain",
        "--review-verdict", "ok",
        "--review-diff-loc", "42",
    ]


def test_validate_review_shape_accepts_legal_list_and_still_emits_one_slice_per_entry():
    """A legal `list[dict]` review must still pass validation and still
    emit one `--review-slice` token per valid entry (mirrors
    `test_build_close_tail_args_directive_list_review_emits_one_slice_per_
    qualifying_entry`, re-asserted post-fix)."""
    decisions = {
        "review": [
            {
                "sha_range": "a1..a2",
                "reviewer": "code-reviewer",
                "scope": "chain",
                "verdict": "ok",
                "diff_loc": 10,
            },
            {
                "sha_range": "b1..b2",
                "reviewer": "staff-eng",
                "scope": "session",
                "verdict": "warn",
                "diff_loc": 20,
            },
        ],
    }
    directive = _dc_tail.build_close_tail_args_directive(decisions)
    assert directive["args"].count("--review-slice") == 2


@pytest.mark.parametrize("falsy_review", [None, {}, [], "", 0, False])
def test_validate_review_shape_stays_silent_on_falsy_review(falsy_review, capsys):
    directive = _dc_tail.build_close_tail_args_directive({"review": falsy_review})
    assert directive["args"] == ["tail-args"]
    assert capsys.readouterr().err == ""

    assert wsc.build_write_trail_directives(falsy_review) == []


# ---------------------------------------------------------------------------
# cross-repo/inbox/2026-08-18-doe-claude-em-review-trail-write-enums-
# undiscoverable-until-apply.md -- the four review enums were reachable ONLY
# by being rejected by them, and the rejection landed at `d-write-trail`,
# which runs FIRST in apply order: a wrong value turned the whole pass into
# `EXIT: 4 PARTIAL_MUTATION`. `_raise_on_review_enum_values` moves that
# verdict to assemble time, where nothing has moved yet.
# ---------------------------------------------------------------------------


def test_illegal_review_enums_are_rejected_at_assemble_time():
    """The exact triple from the memo -- a namespaced reviewer, a `scope`
    filled with a file list, and `scope_kind: files` -- must be rejected by
    the composer, before any directive reaches `directives[]`."""
    decisions = {
        "review": {
            "sha_range": "a..b",
            "reviewer": "coordinator:code-reviewer",
            "scope": "coordinator/hooks/x.py coordinator/tests/y.py",
            "verdict": "ok",
            "scope_kind": "files",
            "diff_loc": 10,
        },
    }
    with pytest.raises(ValueError) as excinfo:
        _dc_tail.build_close_tail_args_directive(decisions)
    message = str(excinfo.value)
    # All three offending fields in ONE raise, not first-wins -- the caller
    # fixes a closed-vocabulary mistake in one round trip, not three.
    assert "reviewer 'coordinator:code-reviewer' is invalid" in message
    assert "scope_kind 'files' is invalid" in message
    assert "scope 'coordinator/hooks/x.py coordinator/tests/y.py' is invalid" in message
    # The bare-name hint survives the move to the earlier seam.
    assert "did you mean 'code-reviewer'?" in message


def test_assemble_time_enum_message_is_byte_identical_to_the_writer_s():
    """Both seams render from `review_enum_errors`, so the text a caller sees
    cannot depend on WHICH gate caught them -- a second, hand-copied allow-list
    in the composer would drift from the writer's silently."""
    from coordinator_core.ops.review_trail_write import review_enum_errors

    writer_errors = review_enum_errors(
        reviewer="staff-eng", scope="partitioned", verdict="ok", scope_kind="diff"
    )
    with pytest.raises(ValueError) as excinfo:
        _dc_tail.build_close_tail_args_directive(
            {
                "review": {
                    "sha_range": "a..b",
                    "reviewer": "staff-eng",
                    "scope": "partitioned",
                    "verdict": "ok",
                    "diff_loc": 10,
                },
            }
        )
    assert len(writer_errors) == 1
    assert writer_errors[0] in str(excinfo.value)
    # `partitioned` is decide_review_scale's vocabulary -- the axis-collision
    # hint is the one the memo's sender would have needed first.
    assert "partition-strategy" in writer_errors[0]


def test_illegal_enum_in_a_review_slice_names_its_index():
    """A list-shaped review names WHICH slice is wrong -- an unindexed message
    across N slices leaves the caller diffing every entry by hand."""
    decisions = {
        "review": [
            dict(_REVIEW_SLICE_A),
            {**dict(_REVIEW_SLICE_B), "scope": "not-a-scope"},
        ],
    }
    with pytest.raises(ValueError) as excinfo:
        _dc_tail.build_close_tail_args_directive(decisions)
    assert "slice[1]" in str(excinfo.value)
    assert "slice[0]" not in str(excinfo.value)


def test_legal_review_enums_compose_exactly_as_before():
    """The gate changes what is REJECTED, never what a legal payload emits."""
    decisions = {
        "review": {
            "sha_range": "abc123..def456",
            "reviewer": "code-reviewer",
            "scope": "session",
            "verdict": "ok",
            "scope_kind": "diff",
            "diff_loc": 42,
        },
    }
    directive = _dc_tail.build_close_tail_args_directive(decisions)
    assert directive["args"] == [
        "tail-args",
        "--review-sha-range", "abc123..def456",
        "--review-reviewer", "code-reviewer",
        "--review-scope", "session",
        "--review-verdict", "ok",
        "--review-diff-loc", "42",
        "--review-scope-kind", "diff",
    ]


def test_incomplete_review_entry_is_not_this_gate_s_class():
    """An entry missing required fields composes no token at all, so its enum
    values never reach the writer -- `_raise_on_review_truthy_unqualified`
    owns that class, and this gate must not pre-empt its message."""
    with pytest.raises(ValueError) as excinfo:
        _dc_tail.build_close_tail_args_directive(
            {"review": {"sha_range": "a..b", "reviewer": "someone"}}
        )
    assert "missing required field(s)" in str(excinfo.value)


def test_build_write_trail_directives_absent_review_key_stays_silent(monkeypatch, tmp_path):
    """`decisions["review"]` genuinely absent (not present-but-malformed)
    must not raise — `brief()`'s own call site passes `decisions.get(
    "review")`, which is `None` when the key is missing entirely."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    ids = {d["id"] for d in decision_object["directives"]}
    assert "d-write-trail" not in ids


# ---------------------------------------------------------------------------
# Lesson capture — structured facets survive the assembler
# ---------------------------------------------------------------------------


def _lesson_add_cli_path() -> Path:
    return Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-lesson-add.py"


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
# gates.completeness_checklist verdict — C0,
# docs/plans/2026-08-18-one-completion-verdict-for-workstream-complete.md.
# `compute_completeness_checklist_gate` had no test coverage anywhere in
# this repo before this chunk; this is its first. Table-driven over the
# gate's four verdict arms (not-applicable x2 / indeterminate / clean /
# open), each pinning `applies`/`unverified_count` to their pre-C0 values —
# `verdict` is additive, not a behavior change.
# ---------------------------------------------------------------------------

_TWO_ITEM_CHECKLIST_TEXT = (
    "---\n"
    'title: "a handoff"\n'
    "completeness_checklist:\n"
    '  - "live: the server responds"\n'
    '  - "restart-gated: config reload takes effect"\n'
    "---\n"
)

_NO_CHECKLIST_TEXT = '---\ntitle: "a handoff"\nstatus: dispatched\n---\n'


@pytest.mark.parametrize(
    "case_name, disposition, consumed_handoff_text, decisions, "
    "expected_applies, expected_unverified, expected_verdict",
    [
        (
            "not-chain-terminal",
            "single-session",
            None,
            {},
            False,
            0,
            "not-applicable",
        ),
        (
            "chain-terminal-no-checklist-field",
            "predecessor-consumed",
            _NO_CHECKLIST_TEXT,
            {},
            False,
            0,
            "not-applicable",
        ),
        (
            "chain-terminal-no-consumed-text",
            "predecessor-consumed",
            None,
            {},
            False,
            0,
            "indeterminate",
        ),
        (
            "all-items-waived",
            "predecessor-consumed",
            _TWO_ITEM_CHECKLIST_TEXT,
            {"waived_items": ["the server responds", "config reload takes effect"]},
            True,
            0,
            "clean",
        ),
        (
            "items-unverified",
            "predecessor-consumed",
            _TWO_ITEM_CHECKLIST_TEXT,
            {},
            True,
            2,
            "open",
        ),
    ],
)
def test_completeness_checklist_gate_four_way_verdict(
    case_name, disposition, consumed_handoff_text, decisions,
    expected_applies, expected_unverified, expected_verdict,
):
    gate = _dc_hygiene.compute_completeness_checklist_gate(
        disposition, consumed_handoff_text, decisions=decisions,
    )
    assert gate.applies is expected_applies, case_name
    assert gate.unverified_count == expected_unverified, case_name
    assert gate.verdict == expected_verdict, case_name


def test_completeness_checklist_gate_archived_away_handoff_is_indeterminate_not_clean():
    """The case C0 exists for: a chain-terminal close whose consumed
    handoff was archived away by the cadence sweeps degrades
    `consumed_handoff_text` to `None` (`__init__._read_consumed_handoff_
    text`'s documented contract) -- the gate had input to look for and
    didn't find it, which is NOT the same as looking and finding
    everything verified."""
    gate = _dc_hygiene.compute_completeness_checklist_gate(
        "predecessor-consumed", None, decisions={},
    )
    assert gate.verdict == "indeterminate"
    assert gate.applies is False
    assert gate.unverified_count == 0


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
    (cross-repo/inbox/2026-08-05-doe-claude-em-plan-tasks-five-exits-
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
    assert gate["verdict"] == "not-applicable"


def test_open_spine_row_gate_silent_when_no_resolvable_plan(monkeypatch, tmp_path):
    """AC3, leg 2: no governing plan resolves at all -> silent no-op."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    gate = decision_object["gates"]["open_spine_row_worklist"]

    assert gate["applies"] is False
    assert gate["warn_text"] is None
    assert gate["summary_line"]
    assert gate["verdict"] == "indeterminate"


def test_open_spine_row_gate_verdict_splits_unresolved_from_genuinely_clean(monkeypatch, tmp_path):
    """The false-clean this gate emitted for a plan-AUTHORING session:
    `applies: False` fired because governing-plan resolution had no input
    at all, and read identically to a spine whose every row was terminal.
    `verdict` is what tells the two apart -- `applies` and `warn_text`
    deliberately still match, because the gate stays advisory and
    non-blocking in both (source memo 2026-08-12-example-market-data-repo-em-
    wsc-capped-a-session-with-an-unexecuted-plan.md)."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(
        tmp_path,
        "all-terminal",
        "- id: C1\n  title: Shipped row\n  disposition: coded\n  disposition_ref: abc123\n",
    )

    resolved = wsc.brief(decisions={"governing_plan_slug": "all-terminal"}, repo_root=tmp_path)
    unresolved = wsc.brief(decisions={}, repo_root=tmp_path)

    resolved_gate = resolved["gates"]["open_spine_row_worklist"]
    unresolved_gate = unresolved["gates"]["open_spine_row_worklist"]

    assert resolved_gate["applies"] == unresolved_gate["applies"] is False
    assert resolved_gate["warn_text"] == unresolved_gate["warn_text"] is None
    assert resolved_gate["verdict"] == "not-applicable"
    assert unresolved_gate["verdict"] == "indeterminate"
    assert resolved_gate["summary_line"] != unresolved_gate["summary_line"]
    assert "INDETERMINATE" in unresolved_gate["summary_line"]

    # Still advisory: an indeterminate verdict adds no judgment point and
    # no exit code -- the memo explicitly did not ask for a refusal.
    assert not any("spine" in jp["id"] for jp in unresolved["judgment_points"])


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
    assert gate["verdict"] == "indeterminate"


def test_open_spine_row_gate_never_blocks_directives_other_than_the_stamp(monkeypatch, tmp_path):
    """AC4 as originally ruled ("never blocks") is now scoped to exactly
    ONE directive: `directives_spine_worklist.compute_open_spine_row_gate`
    itself still computes a purely advisory fact (own docstring/Negative-
    spec unchanged, own module untouched by this fix) and every OTHER
    directive/judgment-point verdict stays byte-identical between a firing
    and a silent run -- proven the same way the prior version of this test
    proved full identity, minus the gate key and minus the one new
    `jp-open-spine-rows-block-stamp` point plus its `d-stamp-plan-
    implemented` dependency edge, which now DO differ between the two runs
    (docs/plans/2026-08-15-composition-invocation-budgets.md was stamped
    `executing -> implemented` with row C2 still `disposition: open`,
    state/kill-ledger.md K-003 -- the incident this narrower gate closes).
    See `test_open_spine_row_gate_blocks_the_implemented_stamp` /
    `test_open_spine_row_gate_waived_row_still_reaches_implemented` for the
    stamp-specific behavior itself."""
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
        # `gates.completion_verdict` (C2) is a downstream aggregate that
        # reads `open_spine_row_worklist`'s own reading -- it legitimately
        # differs between firing/silent for the same reason that gate's
        # own key is popped above, not a byte-identity break.
        gates.pop("completion_verdict")
        judgment_points = [
            jp for jp in decision_object["judgment_points"] if jp["id"] != "jp-open-spine-rows-block-stamp"
        ]
        directives = [dict(d) for d in decision_object["directives"]]
        for directive in directives:
            if directive["id"] != "d-stamp-plan-implemented":
                continue
            depends_on = directive.get("depends_on")
            if isinstance(depends_on, list) and "jp-open-spine-rows-block-stamp" in depends_on:
                remaining = [d for d in depends_on if d != "jp-open-spine-rows-block-stamp"]
                directive["depends_on"] = remaining[0] if len(remaining) == 1 else (remaining or None)
        preflight = dict(decision_object["preflight"])
        decisions_template = dict(preflight["decisions_template"])
        decisions_template.pop("jp-open-spine-rows-block-stamp", None)
        preflight["decisions_template"] = decisions_template
        return {
            "gates": gates,
            # `preflight.decisions_template` legitimately gains a
            # `jp-open-spine-rows-block-stamp` key while firing (every
            # judgment point's id feeds that template) -- popped here for
            # the same reason `narration` is excluded below.
            "preflight": preflight,
            "directives": directives,
            "judgment_points": judgment_points,
            # `narration` is excluded: it echoes the raw judgment-point
            # count ("N judgment point(s)"), which legitimately differs
            # by exactly one between the two runs -- the fact under test
            # here is captured by the `directives`/`judgment_points`
            # equality above, not narration's derived prose.
            "next_move": decision_object["next_move"],
        }

    assert _strip(firing) == _strip(silent)
    firing_jp_ids = {jp["id"] for jp in firing["judgment_points"]}
    silent_jp_ids = {jp["id"] for jp in silent["judgment_points"]}
    assert "jp-open-spine-rows-block-stamp" in firing_jp_ids
    assert "jp-open-spine-rows-block-stamp" not in silent_jp_ids


def test_open_spine_row_gate_blocks_the_implemented_stamp(monkeypatch, tmp_path):
    """Break-class fix: a plan with an unwaived `disposition: open` row
    must not reach `status: implemented` -- `d-stamp-plan-implemented`
    gains a `depends_on` edge onto the new `jp-open-spine-rows-block-stamp`
    judgment point, which names the still-open row id and carries an
    empty `resolves` (unclearable by EM pick, like `jp-commit-subject-
    missing` -- only resolving or waiving the row clears it)."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(
        tmp_path,
        "open-row-plan",
        "- id: C2\n  title: Still unresolved\n  disposition: open\n",
    )
    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "open-row-plan", "subject": "x"}, repo_root=tmp_path
    )

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-open-spine-rows-block-stamp" in jp_ids
    blocking_jp = next(
        jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-open-spine-rows-block-stamp"
    )
    assert "C2" in blocking_jp["question"]
    assert blocking_jp["dispositions"][0]["resolves"] == []

    stamp_directive = next(
        d for d in decision_object["directives"] if d["id"] == "d-stamp-plan-implemented"
    )
    depends_on = stamp_directive["depends_on"]
    depends_on = [depends_on] if isinstance(depends_on, str) else depends_on
    assert "jp-open-spine-rows-block-stamp" in depends_on


def test_open_spine_row_gate_no_open_rows_reaches_implemented_unblocked(monkeypatch, tmp_path):
    """Happy-path regression: every row terminal -> no new judgment point,
    no new `depends_on` edge on `d-stamp-plan-implemented` -- the existing
    `d-claim-plan-execution-lock` dependency (predating this fix) is the
    only one present."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(
        tmp_path,
        "clean-plan",
        "- id: C1\n  title: Shipped row\n  disposition: coded\n  disposition_ref: abc123\n",
    )
    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "clean-plan", "subject": "x"}, repo_root=tmp_path
    )

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-open-spine-rows-block-stamp" not in jp_ids

    stamp_directive = next(
        d for d in decision_object["directives"] if d["id"] == "d-stamp-plan-implemented"
    )
    depends_on = stamp_directive["depends_on"]
    depends_on = [depends_on] if isinstance(depends_on, str) else (depends_on or [])
    assert "jp-open-spine-rows-block-stamp" not in depends_on


def test_open_spine_row_gate_waived_row_still_reaches_implemented(monkeypatch, tmp_path):
    """`decisions["waived_open_spine_row_ids"]` clears the block exactly
    like it already clears the advisory `warn_text` -- a PM-ruled,
    knowingly-carried-open row (e.g. state/kill-ledger.md K-003) is not
    forced through a fabricated disposition to unblock the stamp."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(
        tmp_path,
        "waived-plan",
        "- id: C2\n  title: Ruled dead, blocked on a peer\n  disposition: open\n",
    )
    decision_object = wsc.brief(
        decisions={
            "governing_plan_slug": "waived-plan",
            "subject": "x",
            "waived_open_spine_row_ids": ["C2"],
        },
        repo_root=tmp_path,
    )

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-open-spine-rows-block-stamp" not in jp_ids
    stamp_directive = next(
        d for d in decision_object["directives"] if d["id"] == "d-stamp-plan-implemented"
    )
    depends_on = stamp_directive["depends_on"]
    depends_on = [depends_on] if isinstance(depends_on, str) else (depends_on or [])
    assert "jp-open-spine-rows-block-stamp" not in depends_on


def test_open_spine_row_gate_partial_waiver_names_only_the_unwaived_row(monkeypatch, tmp_path):
    """Two-row fixture, one waived: the block must still fire (the other
    row is genuinely open and unwaived) and must name only C2, never the
    waived C1 -- a single-row fixture cannot distinguish "names the
    unwaived rows" from "names all open rows"; this one can."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(
        tmp_path,
        "partial-waiver-plan",
        "- id: C1\n  title: Reviewed and knowingly left open\n  disposition: open\n"
        "- id: C2\n  title: Still genuinely unresolved\n  disposition: open\n",
    )
    decision_object = wsc.brief(
        decisions={
            "governing_plan_slug": "partial-waiver-plan",
            "subject": "x",
            "waived_open_spine_row_ids": ["C1"],
        },
        repo_root=tmp_path,
    )

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-open-spine-rows-block-stamp" in jp_ids
    blocking_jp = next(
        jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-open-spine-rows-block-stamp"
    )
    assert "C2" in blocking_jp["question"]
    assert "C1" not in blocking_jp["question"]
    assert "C2" in blocking_jp["evidence"]
    assert "C1" not in blocking_jp["evidence"]

    stamp_directive = next(
        d for d in decision_object["directives"] if d["id"] == "d-stamp-plan-implemented"
    )
    depends_on = stamp_directive["depends_on"]
    depends_on = [depends_on] if isinstance(depends_on, str) else depends_on
    assert "jp-open-spine-rows-block-stamp" in depends_on


def test_open_spine_row_gate_indeterminate_still_blocks_the_implemented_stamp(monkeypatch, tmp_path):
    """Correctness fix: `verdict: indeterminate` (malformed spine fence,
    here) also has `warn_text is None`, exactly like the genuinely-clean
    case -- keying the trigger on `warn_text` alone let a terminal
    `implemented` stamp sail through precisely when the spine could not be
    read. The block must fire, and its message must say the spine could
    not be read rather than naming rows it does not have."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(tmp_path, "malformed-stamp-plan", "not: [valid, yaml, - broken")
    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "malformed-stamp-plan", "subject": "x"}, repo_root=tmp_path
    )

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-open-spine-rows-block-stamp" in jp_ids
    blocking_jp = next(
        jp for jp in decision_object["judgment_points"] if jp["id"] == "jp-open-spine-rows-block-stamp"
    )
    assert "could not be read" in blocking_jp["question"]
    assert blocking_jp["dispositions"][0]["resolves"] == []

    stamp_directive = next(
        d for d in decision_object["directives"] if d["id"] == "d-stamp-plan-implemented"
    )
    depends_on = stamp_directive["depends_on"]
    depends_on = [depends_on] if isinstance(depends_on, str) else depends_on
    assert "jp-open-spine-rows-block-stamp" in depends_on


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


# ---------------------------------------------------------------------------
# gates.landed_reconciliation — C3, pln-landed-fires-at-spine-resoluti-ac7e89
# ---------------------------------------------------------------------------


def _write_plan_landed_with_acs(tmp_path: Path, slug: str, ac_lines: str) -> None:
    plan_path = tmp_path / "docs" / "plans" / f"{slug}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "---\ntitle: \"a plan\"\nstatus: landed\n---\n\n"
        "# a plan\n\n## Acceptance Criteria\n\n" + ac_lines + "\n",
        encoding="utf-8",
    )


def test_landed_reconciliation_gate_fires_on_landed_plan_with_open_acs(monkeypatch, tmp_path):
    """AC9: a governing plan at `status: landed` with at least one unticked
    AC -> `applies` true, `warn_text` populated, leads with the action
    (reconcile-and-stamp) rather than a scold, and names no override key."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_landed_with_acs(
        tmp_path,
        "landed-plan",
        "- [x] AC1 — done\n- [ ] AC2 — still open\n",
    )

    decision_object = wsc.brief(decisions={"governing_plan_slug": "landed-plan"}, repo_root=tmp_path)
    gate = decision_object["gates"]["landed_reconciliation"]

    assert gate["applies"] is True
    assert gate["verdict"] == "applicable"
    assert gate["open_count"] == 1
    assert gate["total_count"] == 2
    assert gate["warn_text"] is not None
    assert "landed-plan" in gate["warn_text"]
    assert "Reconcile and stamp now" in gate["warn_text"]
    assert "landed" in gate["warn_text"]
    assert gate["summary_line"]
    # Register (docs/wiki/guard-messaging.md § Register): no override key,
    # no self-legitimacy, no apology, no reassurance wrapper.
    lowered = gate["warn_text"].lower()
    for banned in ("bypass", "override", "sorry", "no need to", "harmless", "not a refusal"):
        assert banned not in lowered


def test_landed_reconciliation_gate_silent_on_landed_and_reconciled_plan(monkeypatch, tmp_path):
    """AC9: a `status: landed` plan whose every AC is ticked is a clean
    close -- silent, not-applicable, never a WARN."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_landed_with_acs(
        tmp_path,
        "reconciled-plan",
        "- [x] AC1 — done\n- [x] AC2 — also done\n",
    )

    decision_object = wsc.brief(decisions={"governing_plan_slug": "reconciled-plan"}, repo_root=tmp_path)
    gate = decision_object["gates"]["landed_reconciliation"]

    assert gate["applies"] is False
    assert gate["warn_text"] is None
    assert gate["verdict"] == "not-applicable"
    assert gate["summary_line"]


def test_landed_reconciliation_gate_silent_on_non_landed_plan(monkeypatch, tmp_path):
    """A plan with open ACs but a non-`landed` status (e.g. still `draft`)
    is out of this gate's scope entirely -- not-applicable, not indeterminate."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    _write_plan_with_spine(
        tmp_path,
        "draft-plan",
        "- id: C1\n  title: Open row\n  disposition: open\n",
    )

    decision_object = wsc.brief(decisions={"governing_plan_slug": "draft-plan"}, repo_root=tmp_path)
    gate = decision_object["gates"]["landed_reconciliation"]

    assert gate["applies"] is False
    assert gate["warn_text"] is None
    assert gate["verdict"] == "not-applicable"


def test_landed_reconciliation_gate_indeterminate_when_no_governing_plan(monkeypatch, tmp_path):
    """No governing plan resolved for this session -> indeterminate, not
    a false-clean not-applicable (mirrors the open-spine-row gate's own
    verdict split)."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    gate = decision_object["gates"]["landed_reconciliation"]

    assert gate["applies"] is False
    assert gate["warn_text"] is None
    assert gate["verdict"] == "indeterminate"
    assert "INDETERMINATE" in gate["summary_line"]


def test_landed_reconciliation_gate_indeterminate_on_landed_plan_with_no_ac_heading(monkeypatch, tmp_path):
    """A `status: landed` plan with no `## Acceptance Criteria` heading at
    all has nothing to reconcile against -- indeterminate, never raises."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    plan_path = tmp_path / "docs" / "plans" / "headless-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "---\ntitle: \"a plan\"\nstatus: landed\n---\n\n# a plan\n\nno AC section here.\n",
        encoding="utf-8",
    )

    decision_object = wsc.brief(decisions={"governing_plan_slug": "headless-plan"}, repo_root=tmp_path)
    gate = decision_object["gates"]["landed_reconciliation"]

    assert gate["applies"] is False
    assert gate["verdict"] == "indeterminate"


def test_landed_reconciliation_gate_blocks_the_implemented_stamp_when_firing(monkeypatch, tmp_path):
    """fourth-instance-hunt.md item 1, Layer A fix: a governing plan
    deliberately parked at `status: landed` with an unticked AC must not
    reach `status: implemented` ungated -- `d-stamp-plan-implemented` gains
    a `depends_on` edge onto the new `jp-landed-reconciliation-block-stamp`
    judgment point, which carries an empty `resolves` (unclearable by EM
    pick -- only reconciling the ACs, so the gate itself goes silent on the
    next `brief()`, clears it). Supersedes the pre-fix AC9 assertion this
    test replaced: `landed_reconciliation` used to be non-blocking by
    design; item 1 promotes it because a `landed` plan is exactly the case
    where `open_spine_row_gate` is silent by construction (every spine row
    has already left `open`), leaving no other contradicting signal."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {"governing_plan_slug": "toggle-landed-plan", "subject": "x"}

    _write_plan_landed_with_acs(tmp_path, "toggle-landed-plan", "- [ ] AC1 — still open\n")
    firing = wsc.brief(decisions=decisions, repo_root=tmp_path)
    assert firing["gates"]["landed_reconciliation"]["applies"] is True

    jp_ids = {jp["id"] for jp in firing["judgment_points"]}
    assert "jp-landed-reconciliation-block-stamp" in jp_ids
    blocking_jp = next(
        jp for jp in firing["judgment_points"] if jp["id"] == "jp-landed-reconciliation-block-stamp"
    )
    assert blocking_jp["dispositions"][0]["resolves"] == []
    assert "1" in blocking_jp["question"]

    stamp_directive = next(
        d for d in firing["directives"] if d["id"] == "d-stamp-plan-implemented"
    )
    depends_on = stamp_directive["depends_on"]
    depends_on = [depends_on] if isinstance(depends_on, str) else depends_on
    assert "jp-landed-reconciliation-block-stamp" in depends_on

    # Wire-path proof, not just envelope shape: `apply`'s real gate
    # (`ceremony_common.apply_halt._directive_gate_open`) must actually
    # refuse to fire the stamp directive with no disposition supplied for
    # the new judgment point -- this is the check whose absence let the
    # original defect survive (envelope-shape-only tests passed either way).
    jp_by_id = {jp["id"]: jp for jp in firing["judgment_points"]}
    directive_ids = {d["id"] for d in firing["directives"]}
    assert apply_halt._directive_gate_open(
        stamp_directive, jp_by_id, decisions={}, directive_ids=directive_ids
    ) is False

    _write_plan_landed_with_acs(tmp_path, "toggle-landed-plan", "- [x] AC1 — now done\n")
    silent = wsc.brief(decisions=decisions, repo_root=tmp_path)
    assert silent["gates"]["landed_reconciliation"]["applies"] is False
    silent_jp_ids = {jp["id"] for jp in silent["judgment_points"]}
    assert "jp-landed-reconciliation-block-stamp" not in silent_jp_ids
    silent_stamp_directive = next(
        d for d in silent["directives"] if d["id"] == "d-stamp-plan-implemented"
    )
    silent_depends_on = silent_stamp_directive["depends_on"]
    silent_depends_on = (
        [silent_depends_on] if isinstance(silent_depends_on, str) else (silent_depends_on or [])
    )
    assert "jp-landed-reconciliation-block-stamp" not in silent_depends_on


def test_landed_reconciliation_gate_indeterminate_blocks_the_implemented_stamp(monkeypatch, tmp_path):
    """Mirrors `test_open_spine_row_gate_indeterminate_still_blocks_the_
    implemented_stamp`: `verdict: indeterminate` (a `landed` plan with no
    `## Acceptance Criteria` heading, here) also has `warn_text is None`,
    exactly like the genuinely-reconciled case -- keying the trigger on
    `warn_text` alone (or on `applies`, which is also False here) would let
    a terminal `implemented` stamp sail through precisely when the
    landed/AC state could not be read. The block must fire, and its
    message must say the state could not be determined rather than naming
    an open/total split it does not have."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    plan_path = tmp_path / "docs" / "plans" / "headless-landed-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "---\ntitle: \"a plan\"\nstatus: landed\n---\n\n# a plan\n\nno AC section here.\n",
        encoding="utf-8",
    )
    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "headless-landed-plan", "subject": "x"}, repo_root=tmp_path
    )

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-landed-reconciliation-block-stamp" in jp_ids
    blocking_jp = next(
        jp for jp in decision_object["judgment_points"]
        if jp["id"] == "jp-landed-reconciliation-block-stamp"
    )
    assert "could not be determined" in blocking_jp["question"]
    assert blocking_jp["dispositions"][0]["resolves"] == []

    stamp_directive = next(
        d for d in decision_object["directives"] if d["id"] == "d-stamp-plan-implemented"
    )
    depends_on = stamp_directive["depends_on"]
    depends_on = [depends_on] if isinstance(depends_on, str) else depends_on
    assert "jp-landed-reconciliation-block-stamp" in depends_on

    jp_by_id = {jp["id"]: jp for jp in decision_object["judgment_points"]}
    directive_ids = {d["id"] for d in decision_object["directives"]}
    assert apply_halt._directive_gate_open(
        stamp_directive, jp_by_id, decisions={}, directive_ids=directive_ids
    ) is False


# ---------------------------------------------------------------------------
# C2a — a blocked judgment point does not abort its siblings (narration)
# ---------------------------------------------------------------------------


def test_next_move_with_open_judgment_points_states_a_block_does_not_abort_siblings():
    """AC4 (C2a): the `next_move` text for >= 1 open judgment point must
    tell the operator that leaving one open only blocks the directives
    that depend on it, not the whole run."""
    gate = _gate("single-session", consumed_handoff_paths=())
    _narration, next_move = wsc._narration_and_next_move(
        gate,
        directives=[{"id": "d-1"}],
        judgment_points=[{"id": "jp-1"}],
    )
    assert "only block" in next_move or "does not abort" in next_move or "not the rest" in next_move
    assert "resolve" in next_move.lower()


def test_next_move_with_no_judgment_points_omits_the_partial_resolution_line():
    """Complement: a brief with no open judgment points never carries the
    partial-resolution sentence — it isn't relevant when there's nothing
    to leave open."""
    gate = _gate("single-session", consumed_handoff_paths=())
    _narration, next_move = wsc._narration_and_next_move(
        gate,
        directives=[{"id": "d-1"}],
        judgment_points=[],
    )
    assert "only block" not in next_move
    assert "not the rest of the run" not in next_move


# ---------------------------------------------------------------------------
# C2b — the replay-safety footnote is scoped, never an unconditional claim
# ---------------------------------------------------------------------------


def test_next_move_states_which_directives_are_verified_replay_safe():
    """AC4 (C2b): with open judgment points AND a verified-re-entrant
    directive present in this run, the next_move names it as safe to
    replay — scoped to what C3 actually verified."""
    gate = _gate("single-session", consumed_handoff_paths=())
    _narration, next_move = wsc._narration_and_next_move(
        gate,
        directives=[{"id": "d-claim-plan-execution-lock"}, {"id": "d-run-wsc-tail"}],
        judgment_points=[{"id": "jp-1"}],
    )
    assert "d-claim-plan-execution-lock" in next_move
    assert "verified safe to replay" in next_move
    assert "re-fire" in next_move


def test_next_move_never_asserts_unconditional_replay_safety():
    """AC4 (C2b): C3's verdict is MIXED, so the brief must never say
    every/nothing-already-landed re-fires as a blanket claim — the
    unconditional line is forbidden regardless of which directives are
    present in a given run."""
    gate = _gate("single-session", consumed_handoff_paths=())
    for directives in (
        [{"id": "d-claim-plan-execution-lock"}],
        [{"id": "d-run-wsc-tail"}],
        [],
    ):
        _narration, next_move = wsc._narration_and_next_move(
            gate,
            directives=directives,
            judgment_points=[{"id": "jp-1"}],
        )
        assert "nothing already landed re-fires" not in next_move
        assert "everything is safe to replay" not in next_move.lower()


def test_next_move_with_no_verified_safe_directives_states_all_reFire():
    """Complement: when no directive in this run is in the verified-safe
    set, the footnote says so plainly rather than silently omitting the
    replay caveat."""
    gate = _gate("single-session", consumed_handoff_paths=())
    _narration, next_move = wsc._narration_and_next_move(
        gate,
        directives=[{"id": "d-run-wsc-tail"}],
        judgment_points=[{"id": "jp-1"}],
    )
    assert "None of this run's directives are verified safe to replay" in next_move
    assert "re-fire" in next_move


# ---------------------------------------------------------------------------
# C4 (docs/plans/2026-08-08-the-engine-asks-for-facts-it-already-holds.md) --
# `decisions_template` threads the SAME `brief()` run's already-resolved
# governing_plan_slug/governing_plan_path/stage_paths values instead of
# nulling them, per the DECISIONS_TEMPLATE_RESOLVED_KEY_ENVELOPE_PATHS
# mapping (AC5); the three corrected false-observation strings (stage-paths
# producer/evidence, completion-entry-scaffold absent-vs-placeholder); and
# the AC8 regression guard that this template-population change did not
# demote any of the five AC3-protected judgment points.
# ---------------------------------------------------------------------------


def test_decisions_template_prefills_governing_plan_slug_and_path_when_resolved(monkeypatch, tmp_path):
    slug = "template-prefill-plan"
    _write_plan(tmp_path, slug)
    _write_handoff(tmp_path, "state/handoffs/x.md", f"docs/plans/{slug}.md")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    resolution = decision_object["preflight"]["governing_plan_resolution"]
    assert resolution["slug"] == slug
    assert resolution["path"] is not None

    template = decision_object["preflight"]["decisions_template"]
    assert template["governing_plan_slug"] == resolution["slug"]
    assert template["governing_plan_path"] == resolution["path"]


def test_decisions_template_governing_plan_keys_stay_none_when_unresolved(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    resolution = decision_object["preflight"]["governing_plan_resolution"]
    assert resolution["slug"] is None
    assert resolution["path"] is None
    template = decision_object["preflight"]["decisions_template"]
    assert template["governing_plan_slug"] is None
    assert template["governing_plan_path"] is None


def test_decisions_template_prefills_stage_paths_from_gates_candidates(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    monkeypatch.setattr(
        wsc.directives_memo_lifecycle,
        "classify_session_authored_files",
        lambda root, start, known_concurrent_paths=frozenset(): [
            {"path": "state/some-file.md", "session_authored": True}
        ],
    )
    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)
    candidates = decision_object["gates"]["stage_paths_candidates"]
    assert candidates == ["state/some-file.md"]
    template = decision_object["preflight"]["decisions_template"]
    assert template["stage_paths"] == candidates


def test_decisions_template_stage_paths_stays_none_when_caller_already_supplied_it(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(
        decisions={"subject": "a commit subject", "stage_paths": ["state/already-known.md"]},
        repo_root=tmp_path,
    )
    assert decision_object["gates"]["stage_paths_candidates"] is None
    template = decision_object["preflight"]["decisions_template"]
    assert template["stage_paths"] is None


def test_build_decisions_template_only_declared_ac5_keys_are_ever_prefilled():
    """Negative-spec: a key not in `DECISIONS_TEMPLATE_RESOLVED_KEY_ENVELOPE_
    PATHS` stays `None` even when `resolved_free_values` supplies a value for
    it -- only the three declared keys are in scope for this chunk."""
    resolved = {
        "governing_plan_slug": "some-plan",
        "governing_plan_path": "docs/plans/some-plan.md",
        "stage_paths": ["a", "b"],
        "review_partition": {"range": "a..b"},  # not a declared key
    }
    template = wsc.build_decisions_template([], resolved)
    assert template["governing_plan_slug"] == "some-plan"
    assert template["governing_plan_path"] == "docs/plans/some-plan.md"
    assert template["stage_paths"] == ["a", "b"]
    assert template["review_partition"] is None


def test_build_decisions_template_declared_mapping_names_the_real_envelope_paths():
    assert wsc.DECISIONS_TEMPLATE_RESOLVED_KEY_ENVELOPE_PATHS == {
        "governing_plan_slug": "preflight.governing_plan_resolution.slug",
        "governing_plan_path": "preflight.governing_plan_resolution.path",
        "stage_paths": "gates.stage_paths_candidates",
    }


def test_stage_paths_missing_evidence_names_the_real_known_concurrent_paths_producer(monkeypatch, tmp_path):
    """Corrected false-observation #1: the evidence/reason no longer claims
    'there is no producer anywhere in this codebase' for known_concurrent_
    paths, and no longer hardcodes known_concurrent_paths=frozenset() when a
    real (non-empty) exclusion set was actually applied."""
    jp = judgments.build_stage_paths_missing_judgment_point(
        ["state/some-file.md"], known_concurrent_paths=frozenset({"state/subagent-share/peer-sid/"})
    )
    assert "there is no producer anywhere in this codebase" not in jp["reason"]
    assert "directives_commit_tail.resolve_known_concurrent_paths" in jp["reason"]
    assert "known_concurrent_paths=frozenset()" not in jp["evidence"]
    assert "state/subagent-share/peer-sid/" in jp["evidence"]


def test_stage_paths_missing_evidence_reports_real_empty_set_when_genuinely_empty():
    jp = judgments.build_stage_paths_missing_judgment_point(["state/some-file.md"], known_concurrent_paths=frozenset())
    assert "known_concurrent_paths=frozenset()" in jp["evidence"]


def test_completion_entry_scaffold_absent_file_reports_not_yet_scaffolded_not_placeholder():
    """Corrected false-observation #2: an entry that does not exist on disk
    is reported as 'not yet scaffolded', never 'still carries placeholder'
    -- there was nothing on disk to carry a placeholder."""
    jp = wsc.build_completion_entry_scaffold_judgment_point(
        "archive/completed/2026-08/2026-08-08-x-abc123.md", ("title", "nature", "prose"), entry_exists=False
    )
    assert "still carries placeholder" not in jp["question"]
    assert "not yet been scaffolded" in jp["question"]
    assert "does not exist on disk yet" in jp["evidence"]


def test_completion_entry_scaffold_existing_file_still_reports_placeholder():
    jp = wsc.build_completion_entry_scaffold_judgment_point(
        "archive/completed/2026-08/2026-08-08-x-abc123.md", ("prose",), entry_exists=True
    )
    assert "still carries placeholder" in jp["question"]
    assert "does not exist on disk yet" not in jp["evidence"]


def test_completion_entry_scaffold_gate_end_to_end_reports_absent_correctly(monkeypatch, tmp_path):
    """End-to-end through brief(): the scaffold gate's own producer resolves
    a not-yet-existing entry path, and the judgment point built from it must
    say so, not claim a placeholder."""
    (tmp_path / "archive").mkdir()
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)
    jp = next(
        j for j in decision_object["judgment_points"] if j["id"] == "jp-completion-entry-scaffold"
    )
    assert "not yet been scaffolded" in jp["question"]
    assert "still carries placeholder" not in jp["question"]


def test_wsc_tail_directive_governing_plan_slug_already_threads_through_effective_decisions(monkeypatch, tmp_path):
    """Corrected false-observation #3: `build_wsc_tail_directive`'s
    `--governing-plan-slug` arg is NOT built from raw, unbackfilled
    `decisions` -- `build_directives`'s own `effective_decisions` backfill
    (governing_plan.slug, when `decisions` didn't already supply it) is
    threaded into `build_wsc_tail_directive`'s call site before this
    directive is built, so a plan resolved via the handoff-frontmatter/
    fixed-fallback legs still reaches `--governing-plan-slug`, not just the
    decisions_slug leg."""
    slug = "wsc-tail-slug-thread-plan"
    _write_plan(tmp_path, slug)
    _write_handoff(tmp_path, "state/handoffs/x.md", f"docs/plans/{slug}.md")
    _patch_gate(monkeypatch, _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()))

    decision_object = wsc.brief(decisions={"subject": "a commit subject"}, repo_root=tmp_path)
    wsc_tail = next(d for d in decision_object["directives"] if d["id"] == "d-run-wsc-tail")
    assert "--governing-plan-slug" in wsc_tail["args"]
    assert slug in wsc_tail["args"]


def test_ac8_regression_five_protected_judgment_points_still_emit(monkeypatch, tmp_path):
    """AC8 regression guard: the five AC3-protected judgment-point ids are
    still emitted as judgment_points after this chunk's decisions_template
    population change -- a template-population change, never new/removed
    behavior for these five."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {
        "review": {
            "sha_range": "a..b",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        },
        "scratch_candidates": ["state/scratch/some-file.md"],
    }
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    for protected_id in (
        "commit-message-authoring",
        "session-work-summary",
        "review-partition-strategy",
        "finding-tradeoff-escalation-check",
        "scratch-disposition-per-file",
    ):
        assert protected_id in jp_ids, f"AC3-protected judgment point {protected_id!r} missing after AC5 change"


# ---------------------------------------------------------------------------
# predecessor-distill-fate gate — must fire only when the predecessor
# genuinely lacks a distill_fate: value to backfill, never on
# disposition == PREDECESSOR_CONSUMED alone (the false-premise defect this
# guards: the judgment point's own question asserts "the predecessor
# handoff lacks distill_fate:" as fact, so firing it against a predecessor
# that HAS declared one silently offers a base-rate `commitment` default
# that overrides the author's own declaration).
# ---------------------------------------------------------------------------


def _write_handoff_with_distill_fate(tmp_path, rel_path: str, distill_fate: str | None) -> None:
    handoff_path = tmp_path / rel_path
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    fate_line = f"distill_fate: {distill_fate}\n" if distill_fate is not None else ""
    handoff_path.write_text(
        f"---\nstatus: open\n{fate_line}---\n\nbody\n",
        encoding="utf-8",
    )


def test_predecessor_distill_fate_point_not_emitted_when_predecessor_declares_it(monkeypatch, tmp_path):
    _write_handoff_with_distill_fate(tmp_path, "state/handoffs/x.md", "ephemeral")
    _patch_gate(
        monkeypatch,
        _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "predecessor-distill-fate" not in jp_ids


def test_predecessor_distill_fate_point_emitted_when_predecessor_has_no_key(monkeypatch, tmp_path):
    _write_handoff_with_distill_fate(tmp_path, "state/handoffs/x.md", None)
    _patch_gate(
        monkeypatch,
        _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "predecessor-distill-fate" in jp_ids


def test_predecessor_distill_fate_point_emitted_when_one_of_plural_set_lacks_it(monkeypatch, tmp_path):
    _write_handoff_with_distill_fate(tmp_path, "state/handoffs/a.md", "ratification")
    _write_handoff_with_distill_fate(tmp_path, "state/handoffs/b.md", None)
    _patch_gate(
        monkeypatch,
        _gate(
            "chain-terminal",
            consumed_handoff="state/handoffs/a.md",
            consumed_handoff_paths=("state/handoffs/a.md", "state/handoffs/b.md"),
        ),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "predecessor-distill-fate" in jp_ids


def test_predecessor_distill_fate_point_emitted_and_no_exception_when_handoff_unreadable(monkeypatch, tmp_path):
    # No handoff written at all -- `state/handoffs/missing.md` resolves to
    # nothing on disk or in the archive leg, so `_predecessor_lacks_distill_
    # fate` must fail open (fires) without raising out of `brief()`.
    _patch_gate(
        monkeypatch,
        _gate("chain-terminal", consumed_handoff="state/handoffs/missing.md", consumed_handoff_paths=()),
    )
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "predecessor-distill-fate" in jp_ids


# ---------------------------------------------------------------------------
# C2 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md) —
# `compute_repo_identity_gate` wired into `brief()` (Site 1) and
# `apply.py::_execute_directives` (Site 2).
#
# AC6: every MISMATCH/UNRESOLVED/MATCH case below is constructed with REAL
# files on disk (a fabricated `<claude-config>/sessions/` registry dir, real
# `.git`-marked directories) — never by monkeypatching `compute_repo_
# identity_gate`'s own return value. Construction pattern reused verbatim
# from `coordinator_core/pickup_assemble/tests/test_repo_identity_gate.py`.
# ---------------------------------------------------------------------------


def _wsc_epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


def _wsc_write_registry_record(sessions_dir, filename, session_id, pid, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _wsc_epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return epoch


def _wsc_make_real_repo(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _wsc_patch_pid_env(monkeypatch, pid, create_time=0.0, hit=True):
    if hit:
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: ((pid, create_time), "env-hit"),
        )
    else:
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: (None, "env-miss:absent"),
        )


def _wsc_patch_stable_pid_alive(monkeypatch, alive=True):
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": alive,
    )


def _wsc_gate_with_sid(sid: str) -> wsc.SessionShapeGate:
    return wsc.SessionShapeGate(
        sid=sid,
        disposition="single-session",
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )


def test_brief_repo_identity_mismatch_refuses_when_repo_root_is_cwd_derived(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _wsc_make_real_repo(repo_root)
    _wsc_make_real_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    sid = "sess-wsc-mismatch"
    _wsc_write_registry_record(sessions_dir, "9001.json", sid, 9001, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _wsc_patch_pid_env(monkeypatch, 9001)
    _wsc_patch_stable_pid_alive(monkeypatch)

    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: _wsc_gate_with_sid(sid))
    # `root` came from `resolve_repo_root()`'s cwd default -- never pass
    # `repo_root` here, per the gate's cwd-derived-only contract.
    monkeypatch.setattr(wsc, "resolve_repo_root", lambda start=None: repo_root)

    with pytest.raises(wsc.TransportFailure):
        wsc.brief(decisions={})


def test_brief_repo_identity_unresolved_does_not_refuse_and_records_verdict(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    _wsc_make_real_repo(repo_root)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _wsc_patch_pid_env(monkeypatch, 9002, hit=False)

    sid = "sess-wsc-unresolved"
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: _wsc_gate_with_sid(sid))
    monkeypatch.setattr(wsc, "resolve_repo_root", lambda start=None: repo_root)

    decision_object = wsc.brief(decisions={})
    assert decision_object["gates"]["repo_identity"]["verdict"] == "UNRESOLVED"


def test_brief_repo_identity_match_records_verdict(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    _wsc_make_real_repo(repo_root)
    sessions_dir = tmp_path / "sessions"
    sid = "sess-wsc-match"
    _wsc_write_registry_record(sessions_dir, "9003.json", sid, 9003, repo_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _wsc_patch_pid_env(monkeypatch, 9003)
    _wsc_patch_stable_pid_alive(monkeypatch)

    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: _wsc_gate_with_sid(sid))
    monkeypatch.setattr(wsc, "resolve_repo_root", lambda start=None: repo_root)

    decision_object = wsc.brief(decisions={})
    assert decision_object["gates"]["repo_identity"]["verdict"] == "MATCH"


def test_brief_explicit_repo_root_never_refused_but_records_informational_verdict(monkeypatch, tmp_path):
    # A MISMATCH-shaped registry record -- an explicitly-supplied `repo_root`
    # must still emit `gates.repo_identity` (informational) but never raise.
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _wsc_make_real_repo(repo_root)
    _wsc_make_real_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    sid = "sess-wsc-explicit"
    _wsc_write_registry_record(sessions_dir, "9004.json", sid, 9004, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _wsc_patch_pid_env(monkeypatch, 9004)
    _wsc_patch_stable_pid_alive(monkeypatch)

    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: _wsc_gate_with_sid(sid))

    decision_object = wsc.brief(decisions={}, repo_root=repo_root)
    assert decision_object["gates"]["repo_identity"]["verdict"] == "MISMATCH"


def _wsc_landing_directive() -> dict[str, Any]:
    return {"id": "d-x", "cli": "wsc-tail", "args": [], "depends_on": None, "already_satisfied": False}


def _wsc_stub_dispatch_directive(monkeypatch) -> None:
    monkeypatch.setattr(
        wsc_apply,
        "_dispatch_directive",
        lambda directive, args=None: {
            "id": directive["id"],
            "cli": directive["cli"],
            "args": list(args if args is not None else directive.get("args", [])),
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        },
    )


def test_apply_execute_directives_repo_identity_mismatch_refuses_when_cwd_derived(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _wsc_make_real_repo(repo_root)
    _wsc_make_real_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    sid = "sess-apply-mismatch"
    _wsc_write_registry_record(sessions_dir, "9101.json", sid, 9101, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _wsc_patch_pid_env(monkeypatch, 9101)
    _wsc_patch_stable_pid_alive(monkeypatch)

    monkeypatch.setattr(wsc_apply, "resolve_repo_root", lambda: repo_root)
    _wsc_stub_dispatch_directive(monkeypatch)

    with pytest.raises(wsc.TransportFailure):
        wsc_apply._execute_directives([_wsc_landing_directive()], [], {}, repo_root=None, sid=sid)


def test_apply_execute_directives_repo_identity_unresolved_does_not_refuse(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    _wsc_make_real_repo(repo_root)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _wsc_patch_pid_env(monkeypatch, 9102, hit=False)

    monkeypatch.setattr(wsc_apply, "resolve_repo_root", lambda: repo_root)
    _wsc_stub_dispatch_directive(monkeypatch)

    exit_code, report = wsc_apply._execute_directives(
        [_wsc_landing_directive()], [], {}, repo_root=None, sid="sess-apply-unresolved"
    )
    assert exit_code == 0
    assert "d-x" in report["landed"]


def test_apply_execute_directives_repo_identity_match_records_and_lands(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    _wsc_make_real_repo(repo_root)
    sessions_dir = tmp_path / "sessions"
    sid = "sess-apply-match"
    _wsc_write_registry_record(sessions_dir, "9103.json", sid, 9103, repo_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _wsc_patch_pid_env(monkeypatch, 9103)
    _wsc_patch_stable_pid_alive(monkeypatch)

    monkeypatch.setattr(wsc_apply, "resolve_repo_root", lambda: repo_root)
    _wsc_stub_dispatch_directive(monkeypatch)

    exit_code, report = wsc_apply._execute_directives(
        [_wsc_landing_directive()], [], {}, repo_root=None, sid=sid
    )
    assert exit_code == 0
    assert "d-x" in report["landed"]


def test_apply_execute_directives_explicit_repo_root_never_refused(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _wsc_make_real_repo(repo_root)
    _wsc_make_real_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    sid = "sess-apply-explicit"
    _wsc_write_registry_record(sessions_dir, "9104.json", sid, 9104, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _wsc_patch_pid_env(monkeypatch, 9104)
    _wsc_patch_stable_pid_alive(monkeypatch)

    _wsc_stub_dispatch_directive(monkeypatch)

    exit_code, report = wsc_apply._execute_directives(
        [_wsc_landing_directive()], [], {}, repo_root=repo_root, sid=sid
    )
    assert exit_code == 0
    assert "d-x" in report["landed"]


# ---------------------------------------------------------------------------
# C4 (docs/plans/2026-08-11-review-trail-carries-execution-basis.md) —
# `_dc_review.chain_partition_execution_basis_report` is a VOICE, not
# a veto: it must report execution basis per discharging record without
# ever changing what `chain_partition_verdict_discharged` discharges.
# ---------------------------------------------------------------------------


def _c4_resolve_range_shas(sha_range: str):
    """Trivial stub resolver: `"<sha>..<sha>"` resolves to `{sha}` alone —
    every fixture record below uses this exact single-commit range shape,
    matching `coverage.SAFE_RANGE`'s accepted `..`/`...` separator."""
    head, _sep, tail = sha_range.partition("..")
    return {tail or head}


_C4_CHAIN_SHA = "a" * 40


def _c4_record(verdict: str, execution_basis=None, scope_kind="diff"):
    record = {
        "verdict": verdict,
        "sha_range": f"{_C4_CHAIN_SHA}~1..{_C4_CHAIN_SHA}",
        "scope_kind": scope_kind,
    }
    if execution_basis is not None:
        record["execution_basis"] = execution_basis
    return record


def test_execution_basis_report_counts_executed_read_only_and_not_recorded():
    records = [
        _c4_record("ok", execution_basis="executed"),
        _c4_record("ok", execution_basis="read-only"),
        _c4_record("ok"),  # field absent — pre-C1 shape
    ]
    report = _dc_review.chain_partition_execution_basis_report(
        records,
        chain_code_shas=[_C4_CHAIN_SHA],
        chain_dag_shas=[_C4_CHAIN_SHA],
        resolve_range_shas=_c4_resolve_range_shas,
    )
    assert report == {
        "executed": 1,
        "read-only": 1,
        _dc_review.EXECUTION_BASIS_NOT_RECORDED: 1,
    }


def test_execution_basis_report_skips_non_discharging_verdicts():
    records = [
        _c4_record("pending", execution_basis="executed"),
        _c4_record("waived", execution_basis="executed"),
    ]
    report = _dc_review.chain_partition_execution_basis_report(
        records,
        chain_code_shas=[_C4_CHAIN_SHA],
        chain_dag_shas=[_C4_CHAIN_SHA],
        resolve_range_shas=_c4_resolve_range_shas,
    )
    assert report == {"executed": 0, "read-only": 0, _dc_review.EXECUTION_BASIS_NOT_RECORDED: 0}


def test_execution_basis_report_empty_when_chain_inputs_unresolved():
    records = [_c4_record("ok", execution_basis="executed")]
    assert _dc_review.chain_partition_execution_basis_report(
        records, chain_code_shas=None, chain_dag_shas=[_C4_CHAIN_SHA], resolve_range_shas=_c4_resolve_range_shas,
    ) == {}
    assert _dc_review.chain_partition_execution_basis_report(
        records, chain_code_shas=[_C4_CHAIN_SHA], chain_dag_shas=None, resolve_range_shas=_c4_resolve_range_shas,
    ) == {}
    assert _dc_review.chain_partition_execution_basis_report(
        records, chain_code_shas=[_C4_CHAIN_SHA], chain_dag_shas=[_C4_CHAIN_SHA], resolve_range_shas=None,
    ) == {}


# AC5's "validate" clause (docs/plans/2026-08-11-review-trail-carries-
# execution-basis.md, C4 body): the 1804-and-growing on-disk
# `state/review-trail/` corpus must continue to validate (not merely stay
# byte-unchanged) through the emit-side collector, which applies the
# quarantine rules (verdict set, timestamp format, required fields). This
# plan adds no key the collector reads — `collect()`'s per-record dict is
# an explicit field-by-field whitelist, so an unknown `execution_basis` key
# on a real record cannot affect quarantine either way (see § Anti-scope's
# schema-surfaces entry: the cockpit-emit record shape and the on-disk
# record shape are already two different surfaces).
#
# Pinned against OBSERVED current behaviour (2026-08-11), per this chunk's
# own instruction — not an assumed count. `state/review-trail/` is a live,
# actively-written corpus on a shared branch; a future re-run growing past
# these totals (more records landing) is expected and not itself evidence
# of a regression, only a divergence from this pin that should be re-based
# against a fresh `git show <merge-base>` baseline rather than treated as a
# failure of this plan's field addition.
#
# RE-BASELINED 2026-08-16 (state/kill-ledger.md K-005, "waiver system dies"):
# K-005 deleted the ~1,913-file `state/review-trail/chain-ancestry-waivers/`
# subtree. `collect()`'s underlying lister walks every `*.json` under
# `state/review-trail/` (see `list_review_trail_records.py`'s directory walk)
# — it was never scoped to review-trail RECORD files specifically, so those
# waiver files (never review-trail records to begin with) were being counted
# as malformed quarantine noise. Their deletion is a one-time ~1,913-file step
# down in `malformed`, not a corpus regression — ~82% of the old 1534 floor
# was this noise. The bare `>=1534` floor this replaced could never survive
# that step (nor any other net deletion from the corpus), so it is replaced
# with a floor/ceiling window sized off the OBSERVED post-K-005 malformed
# count (277) with generous margin, rather than a single point pin — this
# form tolerates ordinary day-to-day corpus churn (new malformed records
# landing, old ones archived) without re-pinning on every run, while still
# catching a collector regression that silently drops or balloons the
# quarantine bucket. `valid`'s floor is untouched — genuine review-trail
# records only accumulate, they were never conflated with the waiver noise.
#
# Finding (report-only, no collector change per this chunk's remit): the
# collector's non-record-file promiscuity above is real and not limited to
# the now-deleted waivers — anything else dropped under `state/review-trail/`
# or `archive/review-trail/` as a stray `*.json` is silently counted as
# "malformed", polluting this number for any other consumer that reads it
# as "malformed REVIEW-TRAIL records" rather than "unparseable JSON files
# found under this directory tree".
def test_real_review_trail_corpus_quarantine_count_unchanged_by_execution_basis_field():
    from coordinator_core.ops.emit.context import EmitContext
    from coordinator_core.ops.emit.sections.review_trail import collect

    repo_root = Path(__file__).resolve().parents[2]
    ctx = EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root / "state",
        git_branch="test",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-01-01T00:00:00Z",
        hostname="test",
        repo_name="test/test",
    )
    valid, malformed = collect(ctx)
    # Observed baseline, 2026-08-16 (post-K-005): 3030 valid, 277 malformed.
    assert len(valid) >= 2011
    assert 100 <= len(malformed) <= 800
    # No key this plan adds is projected into the collected record shape.
    for record in valid:
        assert "execution_basis" not in record


# ---------------------------------------------------------------------------
# C7 (docs/plans/2026-08-12-review-mandate-guides-the-split.md), AC13/AC14 —
# the `commits=` brightline arm stops counting peers. Diagnosis (see this
# chunk's own dispatch brief and sidecar): TWO candidates were live at HEAD,
# both verified present, only ONE reproduced.
#
#   (a) CALLER OVERRIDE, REPRODUCED. `decisions["commit_count"]`
#       (`__init__.py`'s `resolved_commit_count` backfill) wins
#       unconditionally over `_measure_session_review_scale_inputs`'s
#       session-scoped measurement, with zero check the supplied number is
#       session-scoped — exactly the reported shape (an EM reading `commits=`
#       off the gate's own unfiltered range line and passing it through).
#       Landed as the PRE-AUTHORIZED SCOPE-ATTESTATION VARIANT this chunk's
#       brief names: the override still wins (the documented hand-supply
#       affordance, `workstream-complete` SKILL.md, is preserved), but the
#       resolved row-4 `reason` now records the scope it was supplied under
#       (`commit_count_scope=...`, defaulting to `"unspecified"` when the
#       caller supplies `commit_count` with no accompanying
#       `commit_count_scope`) — see `directives_review.decide_review_scale`'s
#       own `commit_count_scope` docstring paragraph.
#
#   (b) TRAILER OVER-MATCH, DID NOT REPRODUCE. Measured directly against
#       this repo's full commit history (14799 commits scanned): every
#       instance where a `Session-Id: <sid>`-shaped LINE appears outside
#       git's own strictly-parsed trailer block (50 instances) is a
#       SAME-SESSION self-match on a malformed trailer block (a blank line,
#       or a `---` section marker, breaking git's trailer-block detection —
#       confirmed via `git interpret-trailers --parse` against several
#       examples), never a body line quoting a DIFFERENT session's trailer.
#       No genuine cross-session over-match was found. No fix lands for (b);
#       `_session_owned_shas` and its documented KNOWN-over-match posture are
#       untouched, per this chunk's own instruction not to fix a candidate
#       that did not reproduce. The interleaved-peer regression test below
#       is therefore a CONFIRMATION of the already-correct measured path
#       (`_session_owned_shas` is trailer-scoped and peer-immune today), not
#       a fix — it guards the measured path against a future regression
#       while the actual reported defect (a) is what the fix above closes.
# ---------------------------------------------------------------------------


def test_measure_session_review_scale_inputs_commit_count_ignores_interleaved_peer_commits(tmp_path):
    """AC13: on a shared branch where a peer commits BETWEEN this session's
    own commits, `_measure_session_review_scale_inputs`'s `commit_count`
    must equal the count of commits THIS session actually authored
    (`Session-Id` trailer match via `_session_owned_shas`), never the length
    of the commit range spanning both sessions. Also demonstrates the
    "idle box" half of AC13: the same own-work commit count is measured
    whether or not peers interleaved commits into the range."""
    own_sid = "11111111-1111-1111-1111-111111111111"
    peer_sid = "22222222-2222-2222-2222-222222222222"
    _init_git_repo(tmp_path)

    # Interleaved: own, peer, own, peer, peer, own -- 3 own commits, 3 peer,
    # on one shared branch (this repo's actual load norm).
    _commit_with_session_trailer(tmp_path, "own-1.md", own_sid)
    _commit_with_session_trailer(tmp_path, "peer-1.md", peer_sid)
    _commit_with_session_trailer(tmp_path, "own-2.md", own_sid)
    _commit_with_session_trailer(tmp_path, "peer-2.md", peer_sid)
    _commit_with_session_trailer(tmp_path, "peer-3.md", peer_sid)
    _commit_with_session_trailer(tmp_path, "own-3.md", own_sid)

    _, _, commit_count, _ = wsc._measure_session_review_scale_inputs(
        tmp_path, session_start_time=None, session_id=own_sid, uncommitted_paths=[]
    )
    assert commit_count == 3, (
        f"expected the session-owned count (3), got {commit_count} -- a range-length "
        "measurement would report 6 (every non-seed commit on the branch)"
    )

    idle_root = tmp_path / "idle"
    idle_root.mkdir()
    _init_git_repo(idle_root)
    _commit_with_session_trailer(idle_root, "own-1.md", own_sid)
    _commit_with_session_trailer(idle_root, "own-2.md", own_sid)
    _commit_with_session_trailer(idle_root, "own-3.md", own_sid)

    _, _, idle_commit_count, _ = wsc._measure_session_review_scale_inputs(
        idle_root, session_start_time=None, session_id=own_sid, uncommitted_paths=[]
    )
    assert idle_commit_count == commit_count == 3, (
        "a session whose peers commit concurrently on the same branch must get the same "
        "commit_count it would get on an idle box"
    )


def test_review_trail_guard_foreign_flag_implies_excluded_from_commits_arm(tmp_path):
    """AC14: ceremony self-consistency, one-directional. No commit the
    review-trail write guard's `trailer_foreign_shas` would flag as
    AFFIRMATIVELY foreign to this session (a `Session-Id` trailer naming a
    DIFFERENT session) is among the shas `_session_owned_shas` credits
    toward the brightline mandate's `commits=` arm. Reuses AC13's
    interleaved-peer synthetic repo (own/peer/own/peer/peer/own) rather than
    authoring a second fixture.

    Deliberately NOT an equivalence assertion. The two mechanisms have
    opposite polarity on an UNTRAILERED commit: `trailer_foreign_shas` is
    exclusion-based (an untrailered commit has no affirmatively-foreign
    trailer, so it stays credited/"not foreign"), while `_session_owned_shas`
    is inclusion-based (an untrailered commit never matches
    `--grep=^Session-Id: <sid>`, so it is never counted as owned either) --
    they disagree by construction there, and this fixture's own commits are
    ALL trailered (own or peer), so no untrailered case appears here. An
    untrailered commit is deliberately OUTSIDE this AC's agreement set: it is
    not exercised or asserted on below, only the one-directional
    flag-implies-excluded relation is."""
    from coordinator_core import session_attribution

    own_sid = "11111111-1111-1111-1111-111111111111"
    peer_sid = "22222222-2222-2222-2222-222222222222"
    _init_git_repo(tmp_path)

    _commit_with_session_trailer(tmp_path, "own-1.md", own_sid)
    _commit_with_session_trailer(tmp_path, "peer-1.md", peer_sid)
    _commit_with_session_trailer(tmp_path, "own-2.md", own_sid)
    _commit_with_session_trailer(tmp_path, "peer-2.md", peer_sid)
    _commit_with_session_trailer(tmp_path, "peer-3.md", peer_sid)
    _commit_with_session_trailer(tmp_path, "own-3.md", own_sid)

    def _run(argv: list[str], cwd: str | None) -> tuple[int, str, str]:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    foreign_shas = session_attribution.trailer_foreign_shas(
        sha_range="HEAD",
        own_session_id=own_sid,
        cwd=str(tmp_path),
        cache={},
        run=_run,
    )
    # All 3 peer commits carry an affirmatively-foreign Session-Id trailer;
    # the seed commit (no trailer at all) is untrailered, not foreign, and
    # stays outside this set -- exclusion-based polarity, per docstring.
    assert len(foreign_shas) == 3

    owned_shas = wsc._session_owned_shas(tmp_path, own_sid)
    assert owned_shas is not None and len(owned_shas) == 3

    # The one-directional AC14 relation: flagged-affirmatively-foreign
    # implies not-counted toward the commits= arm. Never asserted the
    # converse (not-flagged implies counted) -- that fails by construction
    # on an untrailered commit, which is outside this AC's agreement set.
    for sha in foreign_shas:
        assert sha not in owned_shas, (
            f"{sha} was flagged affirmatively foreign by the review-trail write "
            "guard's trailer_foreign_shas, yet was also credited toward the "
            "commits= arm by _session_owned_shas -- AC14 violated"
        )


def test_commit_count_override_wins_unconditionally_and_records_supplied_scope(monkeypatch, tmp_path):
    """C7 candidate (a), reproduction + fix. Reproduction: `tmp_path` here
    is not even a git repo (measurement fails closed to `None`), yet the
    caller-supplied override still resolves row 4 -- proof the override path
    never consults, and is never checked against, the measured value. Fix
    (pre-authorized scope-attestation variant): the override still wins
    (preserving `workstream-complete` SKILL.md's documented hand-supply
    affordance), but the resolved reason now records the scope it was
    supplied under."""
    _patch_gate(monkeypatch, _gate("single-session"))
    decision_object = wsc.brief(
        decisions={"commit_count": 21, "commit_count_scope": "gate-unfiltered-range"},
        repo_root=tmp_path,
    )
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["resolved"] is True
    assert review_scale["row"] == 4
    assert "commits=21" in review_scale["reason"]
    assert "commit_count_scope=gate-unfiltered-range" in review_scale["reason"]


def test_commit_count_override_without_scope_records_unspecified(monkeypatch, tmp_path):
    """The override is never refused for lacking a scope note -- refusing or
    removing the override outright is the one path this chunk's brief
    narrows to a PM call, not authorized here. An unattested override still
    lands, but is recorded as `commit_count_scope=unspecified` rather than
    silently read as equivalent to a real session-scoped measurement."""
    _patch_gate(monkeypatch, _gate("single-session"))
    decision_object = wsc.brief(decisions={"commit_count": 21}, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["row"] == 4
    assert "commit_count_scope=unspecified" in review_scale["reason"]


def test_commit_count_measured_path_carries_no_scope_clause(monkeypatch, tmp_path):
    """The measured (non-override) path must stay byte-identical to every
    pre-2026-08-12 caller: no `commit_count_scope=` clause appears when the
    session's own trailer-scoped measurement resolved `commit_count`, never
    a caller override."""
    own_sid = "testsid123"  # matches `_gate()`'s fixed sid
    _init_git_repo(tmp_path)
    for i in range(6):
        name = f"f{i}.md"
        (tmp_path / name).write_text(f"{i}\n", encoding="utf-8")
        subprocess.run(["git", "add", name], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"c{i}", "--trailer", f"Session-Id: {own_sid}"],
            cwd=tmp_path,
            check=True,
        )
    _patch_gate(monkeypatch, _gate("single-session"))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    review_scale = decision_object["gates"]["review_scale"]
    assert review_scale["row"] == 4, "6 own commits >= _BRIGHTLINE_COMMITS(5) must still trip row 4"
    assert "commits=6" in review_scale["reason"]
    assert "commit_count_scope" not in review_scale["reason"]


def test_commit_count_scope_strips_unsafe_characters_and_caps_length(monkeypatch, tmp_path):
    """P2 (review-integrator, 2026-08-12): `commit_count_scope` is
    caller-supplied free text threaded verbatim into row 4's `reason`
    string. A value containing `)`/`=`/control characters could otherwise
    forge a second `commits=`/`surfaces=` clause or close the parenthetical
    early; `_sanitize_commit_count_scope` strips those and caps length."""
    _patch_gate(monkeypatch, _gate("single-session"))
    decision_object = wsc.brief(
        decisions={
            "commit_count": 21,
            "commit_count_scope": "session-owned) surfaces=99 (commits=1",
        },
        repo_root=tmp_path,
    )
    review_scale = decision_object["gates"]["review_scale"]
    reason = review_scale["reason"]
    scope_clause = reason.split("commit_count_scope=", 1)[1].rstrip(")")
    assert "commit_count_scope=session-owned surfaces99 commits1" in reason
    assert ")" not in scope_clause
    assert "=" not in scope_clause


def test_commit_count_scope_caps_length(monkeypatch, tmp_path):
    """P2 (review-integrator, 2026-08-12): an over-long `commit_count_scope`
    is truncated to `_COMMIT_COUNT_SCOPE_MAX_LEN`, not stored unbounded."""
    _patch_gate(monkeypatch, _gate("single-session"))
    overlong = "x" * 500
    decision_object = wsc.brief(
        decisions={"commit_count": 21, "commit_count_scope": overlong},
        repo_root=tmp_path,
    )
    review_scale = decision_object["gates"]["review_scale"]
    scope_clause = review_scale["reason"].split("commit_count_scope=", 1)[1].rstrip(")")
    assert len(scope_clause) == wsc._COMMIT_COUNT_SCOPE_MAX_LEN


# ---------------------------------------------------------------------------
# gates.completion_verdict — C2,
# docs/plans/2026-08-18-one-completion-verdict-for-workstream-complete.md.
# `compose_completion_verdict` itself is tested directly (table-driven, over
# synthetic `GateReading`s) for exact control of the composition-rule
# arms; the `brief()`-level tests confirm the key's real presence/shape on
# the envelope and that this chunk changes no judgment-point behavior
# (AC6).
# ---------------------------------------------------------------------------

_CV_CLEAN = _cv.GateReading(status="clean", residue_items=(), reason=None)
_CV_OPEN = _cv.GateReading(status="open", residue_items=(), reason=None)
_CV_NA = _cv.GateReading(status="not-applicable", residue_items=(), reason=None)
_CV_INDET = _cv.GateReading(status="indeterminate", residue_items=(), reason=None)


def _cv_readings(
    completeness_checklist=_CV_NA,
    open_spine_row_worklist=_CV_NA,
    consumed_handoff_completeness=_CV_NA,
    landed_reconciliation=_CV_NA,
    review_scale=_CV_NA,
):
    return {
        "completeness_checklist": completeness_checklist,
        "open_spine_row_worklist": open_spine_row_worklist,
        "consumed_handoff_completeness": consumed_handoff_completeness,
        "landed_reconciliation": landed_reconciliation,
        "review_scale": review_scale,
    }


def test_completion_verdict_ac8_all_four_not_applicable_is_indeterminate_never_complete():
    """AC8: nothing was measured (all four census gates `not-applicable`),
    so `verdict` must be `indeterminate`, never `complete` -- `complete`
    requires positive evidence (>=1 `clean`)."""
    payload = _cv.compose_completion_verdict(_cv_readings())
    assert payload["verdict"] == "indeterminate"
    assert payload["clean_count"] == 0
    assert payload["not_applicable_count"] == 4
    assert payload["indeterminate_gates"] == []


@pytest.mark.parametrize(
    "readings_kwargs, expected_verdict",
    [
        ({"completeness_checklist": _CV_CLEAN}, "complete"),
        ({"completeness_checklist": _CV_CLEAN, "landed_reconciliation": _CV_NA}, "complete"),
        ({"completeness_checklist": _CV_OPEN}, "incomplete"),
        ({"completeness_checklist": _CV_CLEAN, "open_spine_row_worklist": _CV_OPEN}, "incomplete"),
        ({"completeness_checklist": _CV_INDET}, "indeterminate"),
        (
            {"completeness_checklist": _CV_CLEAN, "landed_reconciliation": _CV_INDET},
            "indeterminate",
        ),
        (
            {"completeness_checklist": _CV_OPEN, "landed_reconciliation": _CV_INDET},
            "incomplete",
        ),
    ],
)
def test_completion_verdict_composition_rule_table(readings_kwargs, expected_verdict):
    """The composition rule, over the four census gates only: any `open`
    wins outright (`incomplete`), else `complete` requires >=1 `clean` and
    zero `indeterminate`, else `indeterminate`."""
    payload = _cv.compose_completion_verdict(_cv_readings(**readings_kwargs))
    assert payload["verdict"] == expected_verdict


def test_completion_verdict_review_scale_excluded_from_verdict_and_census():
    """`review_scale` is narration-only (F5): an `open`- or `indeterminate`-
    shaped review_scale reading must never flip `verdict`, appear in
    `indeterminate_gates[]`, or count toward `clean_count`/
    `not_applicable_count` -- only the four census gates do."""
    all_clean_except_review_scale = _cv_readings(
        completeness_checklist=_CV_CLEAN, review_scale=_CV_INDET,
    )
    payload = _cv.compose_completion_verdict(all_clean_except_review_scale)
    assert payload["verdict"] == "complete"
    assert "review_scale" not in payload["indeterminate_gates"]
    assert payload["clean_count"] == 1
    assert payload["not_applicable_count"] == 3

    review_scale_open_never_forces_incomplete = _cv_readings(
        completeness_checklist=_CV_CLEAN, review_scale=_CV_OPEN,
    )
    payload = _cv.compose_completion_verdict(review_scale_open_never_forces_incomplete)
    assert payload["verdict"] == "complete"

    # `review_scale` still appears in `readings[]` for narration.
    reading_gates = {r["gate"] for r in payload["readings"]}
    assert "review_scale" in reading_gates


def test_completion_verdict_indeterminate_gates_populated_even_when_verdict_is_incomplete():
    """`indeterminate_gates[]` is ALWAYS populated with every indeterminate
    census gate, independent of the top-level verdict -- including when
    `verdict` is `incomplete` (a concrete `open` reading elsewhere must not
    make other unreadable gates disappear from the envelope)."""
    payload = _cv.compose_completion_verdict(
        _cv_readings(
            completeness_checklist=_CV_OPEN,
            open_spine_row_worklist=_CV_INDET,
            landed_reconciliation=_CV_INDET,
        )
    )
    assert payload["verdict"] == "incomplete"
    assert set(payload["indeterminate_gates"]) == {"open_spine_row_worklist", "landed_reconciliation"}


def test_completion_verdict_residue_concatenates_every_readings_residue_items():
    open_reading = _cv.GateReading(
        status="open",
        residue_items=({"gate": "completeness_checklist", "reference": "x", "summary": "s"},),
        reason=None,
    )
    payload = _cv.compose_completion_verdict(_cv_readings(completeness_checklist=open_reading))
    assert payload["residue"] == [{"gate": "completeness_checklist", "reference": "x", "summary": "s"}]


def test_completion_verdict_present_on_brief_envelope_with_verdict_enum_and_indeterminate_gates(
    monkeypatch, tmp_path
):
    """AC1: `gates.completion_verdict` is present on every `brief()`
    envelope, carrying a `verdict` in the declared enum and an ALWAYS-
    populated `indeterminate_gates[]` -- verified over a fixture session
    with no governing plan resolved (open_spine_row_worklist and
    landed_reconciliation both read `indeterminate` in that shape, per
    their own gates' "no governing plan resolved" arm), doubling as this
    chunk's AC8 end-to-end case: no reading is `clean`, so `verdict` is
    `indeterminate`, never `complete`."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
    completion_verdict = decision_object["gates"]["completion_verdict"]
    assert completion_verdict["verdict"] in {"complete", "incomplete", "indeterminate"}
    assert completion_verdict["verdict"] == "indeterminate"
    assert completion_verdict["clean_count"] == 0
    assert isinstance(completion_verdict["indeterminate_gates"], list)
    assert set(completion_verdict["indeterminate_gates"]) == {
        "open_spine_row_worklist",
        "landed_reconciliation",
    }
    reading_gates = {r["gate"] for r in completion_verdict["readings"]}
    assert reading_gates == {
        "completeness_checklist",
        "open_spine_row_worklist",
        "consumed_handoff_completeness",
        "landed_reconciliation",
        "review_scale",
    }


def test_completion_verdict_ac6_no_new_judgment_point_and_ids_unchanged(monkeypatch, tmp_path):
    """AC6: this plan gates nothing -- no `depends_on` edge, no judgment
    point, no halt/block. Regression guard: the judgment-point id set on a
    fixture brief (the same fixture `test_ac8_regression_five_protected_
    judgment_points_still_emit` above uses) is exactly what it was before
    this chunk, with no `completion-verdict`-shaped id added."""
    _patch_gate(monkeypatch, _gate("single-session", consumed_handoff_paths=()))
    decisions = {
        "review": {
            "sha_range": "a..b",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        },
        "scratch_candidates": ["state/scratch/some-file.md"],
    }
    decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert jp_ids == {
        "commit-message-authoring",
        "commit-significance-filter",
        "finding-tradeoff-escalation-check",
        "governing-spec-identification",
        "jp-commit-subject-missing",
        "jp-review-scale",
        "jp-stage-paths-missing",
        "lesson-worth-capturing",
        "quota-retry-vs-escalate",
        "review-dispatch-vehicle-choice",
        "review-partition-strategy",
        "reviewer-count-on-oracle-disagreement",
        "scratch-disposition-per-file",
        "session-work-summary",
        "shallow-row3-waive-check",
        "shared-schema-touch-check",
    }
    assert not any("completion-verdict" in jp_id or "completion_verdict" in jp_id for jp_id in jp_ids)
