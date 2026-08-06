"""
coordinator_core.ops.tests.test_handoff_reconcile_gate_evidence

Coverage for the `_AWAITING_GATE_STATE` branch of `handoff.reconcile_open`
threading each handoff's `gate_evidence:` block into `evaluate_gate` (PM
ruling 2026-08-01: the sweep may CONSUME gate_evidence to compute a better
answer; it may never use that answer to flip a gate itself).

Uses `monkeypatch` on the module-level `_read_gate_evidence_resolved` binding
(imported into `handoff_reconcile` from `handoff_transition`) rather than
authoring real sibling-fact I/O-kind legs on disk — `_reresolve_gate_evidence_
leg`'s live re-resolution of I/O-kind legs (kind in `_SIBLING_FACT_KIND_FOR_
ON_DISK_KIND`) requires a real sibling-repo registry entry to observe
anything other than `read_ok: False`, which is out of scope here: this suite
tests `handoff_reconcile`'s OWN wiring/routing of an already-resolved
`gate_evidence` block, not `gate_eval`'s or `sibling_fact`'s own leg
resolution (each has its own dedicated test coverage).

Spec backlink: cross-repo dispatch brief, 2026-08-01, "surface-only gate
sweep" (PM ruling: gate_evidence may inform the sweep's verdict, never flip
a gate automatically).

Negative-spec:
    - Does NOT exercise `gate_eval.py` rule (2) (blocker in
      abandoned|continued|closed -> surface, never clear) — out of scope,
      under an open doctrine question with example-doctrine-repo per the dispatch brief.
    - Does NOT test `_read_gate_evidence_resolved`'s own on-disk read/live-leg
      -resolution behavior — that lives with `handoff_transition`'s (and,
      pre-lift, `handoff_gate_aging`'s) own coverage.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

import coordinator_core.ops.handoff_reconcile  # noqa: F401 — fires @register_op
from coordinator_core.ops import handoff_reconcile
from coordinator_core.ops.handoff_reconcile import _handler
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter


def _run(coro):
    return asyncio.run(coro)


def _write_enabled_policy(tmp_path: Path) -> str:
    """Grammar-valid policy fixture with `dry_run: False` — arms act-mode
    for tests checking the surface-only invariant holds even when the
    sweep is authorized to transition. Mirrors
    `test_handoff_reconcile._write_enabled_policy` (not imported directly —
    no cross-test-module import precedent in this suite)."""
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    policy_file.write_text(
        yaml.safe_dump({
            "three_signal": {},
            "mechanical_commit_denylist": [
                "pickup:", "reclaim(docs)", "session-init", "memo:", "handoff.transition",
            ],
            "cross_handoff_attribution": True,
            "dry_run": False,
        }),
        encoding="utf-8",
    )
    return str(policy_file)


def _deployment_state(repo, name: str) -> str:
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field(split.fm_text, "deployment_state")


def _fake_reader(mapping):
    """Return a `_read_gate_evidence_resolved`-shaped stand-in keyed by file
    basename — see module docstring for why this is monkeypatched rather
    than authored as real on-disk I/O-kind legs."""

    def _reader(path, today):  # noqa: ARG001 — today unused, matches real signature
        return mapping.get(Path(path).name)

    return _reader


def test_resolving_gate_evidence_surfaces_as_evidence_resolved_not_auto_flipped(
    handoff_repo, monkeypatch,
):
    """A handoff carrying `gate_evidence` whose legs AND-reduce to `freed`
    (evaluate_gate rule 0 -> verdict='clear') must land in surfaced[] marked
    `gate_evidence_resolved: True`, and must NOT be routed to
    `_handle_gate_cascade` — gates_cleared[] stays empty and the on-disk
    deployment_state is untouched, even under the default dry_run=True."""
    handoff_repo.seed_handoff(
        "2026-02-01-gate-evidence-clear.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-gate-evidence-clear.md": {
                "covers_prose": True,
                "legs": [
                    {"leg_id": "leg1", "kind": "commit-ancestor", "observed": True},
                ],
            },
        }),
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    assert result["gates_cleared"] == [], result["gates_cleared"]
    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-gate-evidence-clear")
    assert entry is not None, result["surfaced"]
    assert entry["gate_evidence_resolved"] is True
    assert "clear" in entry["reason"]
    assert (
        _deployment_state(handoff_repo, "2026-02-01-gate-evidence-clear.md")
        == "awaiting_gate"
    )


def test_evidence_resolved_clear_verdict_never_flips_gate_even_in_act_mode(
    handoff_repo, tmp_path, monkeypatch,
):
    """The surface-only invariant must hold even when the policy arms
    act-mode (dry_run: False) — an evidence-backed 'clear' verdict is still
    forced onto the surfaced[]-only path, never `_handle_gate_cascade`."""
    handoff_repo.seed_handoff(
        "2026-02-01-gate-evidence-clear-act.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )
    policy_path = _write_enabled_policy(tmp_path)

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-gate-evidence-clear-act.md": {
                "covers_prose": True,
                "legs": [
                    {"leg_id": "leg1", "kind": "commit-ancestor", "observed": True},
                ],
            },
        }),
    )

    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["gates_cleared"] == [], result["gates_cleared"]
    surfaced_ids = {s["handoff_id"] for s in result["surfaced"]}
    assert "2026-02-01-gate-evidence-clear-act" in surfaced_ids
    assert (
        _deployment_state(handoff_repo, "2026-02-01-gate-evidence-clear-act.md")
        == "awaiting_gate"
    ), "an evidence-backed clear verdict must never flip deployment_state on disk"


def test_handoff_without_gate_evidence_block_behaves_as_before(handoff_repo):
    """No `_read_gate_evidence_resolved` monkeypatch here — the real reader
    runs against a handoff with plain prose `gate_dependency` and no
    `gate_evidence:` block at all, and must surface exactly as it did before
    this change: plain 'gate_eval verdict=surface', no
    `gate_evidence_resolved` marker."""
    handoff_repo.seed_handoff(
        "2026-02-01-plain-prose-gate.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "some unresolved subsystem work"',
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-plain-prose-gate")
    assert entry is not None, result["surfaced"]
    assert entry["reason"] == "gate_eval verdict=surface"
    assert "gate_evidence_resolved" not in entry


def test_evidence_only_gate_no_prose_surfaces_and_does_not_flip_in_act_mode(
    handoff_repo, tmp_path, monkeypatch,
):
    """The exact gap the first landing of this guard missed: a handoff with
    NO prose `gate_dependency` (the intended post-deprecation shape —
    `handoff.schema.json` marks `gate_dependency` DEPRECATED) and NO
    `blocked_by` falls through `evaluate_gate` to the vacuous-clear branch,
    which does not consult `gate_evidence` at all. Keying the surface-only
    guard on `has_prose` would let this verdict flow straight into
    `_handle_gate_cascade`. Asserts it surfaces instead — with
    `gate_evidence_resolved: False` (the vacuous verdict was NOT actually
    evidence-derived) — and the on-disk deployment_state is untouched, even
    under an armed (`dry_run: False`) policy."""
    handoff_repo.seed_handoff(
        "2026-02-01-gate-evidence-only-no-prose.md", "open",
        deployment_state="awaiting_gate",
    )
    policy_path = _write_enabled_policy(tmp_path)

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-gate-evidence-only-no-prose.md": {
                "covers_prose": False,
                "legs": [
                    {"leg_id": "leg1", "kind": "commit-ancestor", "observed": True},
                ],
            },
        }),
    )

    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["gates_cleared"] == [], result["gates_cleared"]
    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-gate-evidence-only-no-prose")
    assert entry is not None, result["surfaced"]
    assert entry["gate_evidence_resolved"] is False
    assert "gate_evidence present" in entry["reason"]
    assert (
        _deployment_state(handoff_repo, "2026-02-01-gate-evidence-only-no-prose.md")
        == "awaiting_gate"
    ), "an evidence-only gate must never auto-flip, even when the sweep is armed"


def test_multi_leg_gate_evidence_block_survives_the_read_intact(
    handoff_repo, monkeypatch,
):
    """A multi-leg `gate_evidence` block (one satisfied, one indeterminate)
    must reach `evaluate_gate` with every leg intact — the AND-reduce's
    'indeterminate wins' precedence forces verdict='surface', and the
    per-leg evidence strings for BOTH legs must be present in the surfaced
    entry's evidence, proving no leg was silently dropped in transit."""
    handoff_repo.seed_handoff(
        "2026-02-01-gate-evidence-multi-leg.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-gate-evidence-multi-leg.md": {
                "covers_prose": True,
                "legs": [
                    {"leg_id": "leg-satisfied", "kind": "commit-ancestor", "observed": True},
                    {"leg_id": "leg-human", "kind": "human", "reason": "needs a human look"},
                ],
            },
        }),
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-gate-evidence-multi-leg")
    assert entry is not None, result["surfaced"]
    assert entry["gate_evidence_resolved"] is True
    assert "surface" in entry["reason"]
    evidence_text = " | ".join(entry["evidence"])
    assert "leg-satisfied" in evidence_text
    assert "leg-human" in evidence_text
    assert (
        _deployment_state(handoff_repo, "2026-02-01-gate-evidence-multi-leg.md")
        == "awaiting_gate"
    )


def test_narrow_verdict_with_gate_evidence_surfaces_with_narrow_reason_text(
    handoff_repo, monkeypatch,
):
    """Review: code-reviewer Finding 4 -- the `verdict == 'narrow'` branch of
    the surface-only interception (distinct reason-text f-string from
    'clear') was previously untested. `evaluate_gate` is monkeypatched
    directly (real gate_evidence-driven verdicts are only ever clear/surface
    -- `narrow` is a structured-`blocked_by`-only verdict, see
    `gate_eval._gate_evidence_status_to_verdict`) so this suite can exercise
    `handoff_reconcile`'s own routing of a narrow verdict on an
    evidence-carrying handoff without needing to construct a real structured
    narrow scenario."""
    handoff_repo.seed_handoff(
        "2026-02-01-gate-evidence-narrow.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-gate-evidence-narrow.md": {
                "covers_prose": True,
                "legs": [
                    {"leg_id": "leg1", "kind": "commit-ancestor", "observed": True},
                ],
            },
        }),
    )
    monkeypatch.setattr(
        handoff_reconcile,
        "evaluate_gate",
        lambda *args, **kwargs: {"verdict": "narrow", "evidence": ["narrow stub"]},
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    assert result["gates_cleared"] == [], result["gates_cleared"]
    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-gate-evidence-narrow")
    assert entry is not None, result["surfaced"]
    assert entry["gate_evidence_resolved"] is True
    assert "narrow-mutation candidate" in entry["reason"]
    assert (
        _deployment_state(handoff_repo, "2026-02-01-gate-evidence-narrow.md")
        == "awaiting_gate"
    )


def test_blocking_notes_dominance_reports_gate_evidence_not_resolved(
    handoff_repo, monkeypatch,
):
    """Review: code-reviewer Finding 2/3 -- a handoff carrying BOTH a
    non-empty `blocking_notes` AND a `gate_evidence` block with
    `covers_prose: True` must report `gate_evidence_resolved: False`.
    `blocking_notes` dominance (gate_eval.py's own precedence, checked
    ahead of rule 0) intercepts the verdict before `gate_evidence` is ever
    consulted -- a locally-reimplemented predicate that didn't consider
    `blocking_notes` previously mislabeled this `True`. Uses the REAL
    `evaluate_gate` (not monkeypatched) so gate_eval's own precedence
    decides the verdict, proving `handoff_reconcile`'s `evidence_consumed`
    now agrees with it."""
    handoff_repo.seed_handoff(
        "2026-02-01-blocking-notes-dominance.md", "open",
        deployment_state="awaiting_gate",
        extra=(
            'gate_dependency: "sibling repo ships the widgetforge feature"\n'
            'blocking_notes: "still waiting on a human call"'
        ),
    )

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-blocking-notes-dominance.md": {
                "covers_prose": True,
                "legs": [
                    {"leg_id": "leg1", "kind": "commit-ancestor", "observed": True},
                ],
            },
        }),
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-blocking-notes-dominance")
    assert entry is not None, result["surfaced"]
    assert entry["gate_evidence_resolved"] is False, (
        "blocking_notes dominance intercepts before gate_evidence is ever "
        "consulted -- must not be reported as evidence-resolved"
    )
    assert (
        _deployment_state(handoff_repo, "2026-02-01-blocking-notes-dominance.md")
        == "awaiting_gate"
    )


def test_satisfied_structured_blocked_by_with_blocking_notes_still_reports_evidence_consumed(
    handoff_repo, monkeypatch,
):
    """AC5.1 (post-C4 under-report): a handoff carrying a NON-EMPTY, FULLY
    SATISFIED `blocked_by`, a non-empty `blocking_notes`, a prose
    `gate_dependency`, and a `gate_evidence` block with `covers_prose: True`
    now DOES reach `evaluate_gate`'s rule 0 -- the demoted `blocking_notes`
    rule 1a (module docstring "BLOCKING_NOTES DOMINANCE") only intercepts an
    EMPTY `blocked_by`. `handoff_reconcile` must report `gate_evidence_
    resolved: True` here. Uses the REAL `evaluate_gate` (not monkeypatched)
    so gate_eval's own precedence decides the verdict -- this is the
    scenario the pre-fix inline `evidence_consumed` expression
    (`_has_prose_gate(h) and not _has_blocking_notes(h) and ...`)
    mislabeled `False`, since it treated ANY non-empty `blocking_notes` as an
    unconditional veto regardless of `blocked_by` state."""
    handoff_repo.seed_handoff(
        "2026-02-01-ac51-blocker.md", "open",
        deployment_state="shipped",
        shipped_in="a" * 40,
        extra='id: "ac51-blocker-1"',
    )
    handoff_repo.seed_handoff(
        "2026-02-01-ac51-dependent.md", "open",
        deployment_state="awaiting_gate",
        extra=(
            'gate_dependency: "sibling repo ships the widgetforge feature"\n'
            'blocking_notes: "still waiting on a human call"\n'
            'blocked_by:\n  - "ac51-blocker-1"'
        ),
    )

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-ac51-dependent.md": {
                "covers_prose": True,
                "legs": [
                    {"leg_id": "leg1", "kind": "commit-ancestor", "observed": True},
                ],
            },
        }),
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-ac51-dependent")
    assert entry is not None, result["surfaced"]
    assert entry["gate_evidence_resolved"] is True, (
        "a satisfied non-empty blocked_by must not be treated as the vacuous "
        "case -- blocking_notes no longer dominates here, so evaluate_gate "
        "reaches rule 0 and consults gate_evidence"
    )
    assert "clear" in entry["reason"]
    assert result["gates_cleared"] == [], result["gates_cleared"]
    assert (
        _deployment_state(handoff_repo, "2026-02-01-ac51-dependent.md")
        == "awaiting_gate"
    )


def test_unfilled_scaffold_sentinel_with_covering_evidence_reports_evidence_not_consumed(
    handoff_repo, monkeypatch,
):
    """AC5.2 (sentinel over-report): a handoff whose `gate_dependency` is an
    unfilled `coordinator-doc-new` scaffold placeholder (`PLACEHOLDER`) makes
    `_has_prose_gate` return True (a non-empty string), so the OLD inline
    `evidence_consumed` expression reported `True` here -- but
    `evaluate_gate` short-circuits at rule SC (module docstring "C2 SCAFFOLD
    SENTINEL") and never consults `gate_evidence` at all. Must report
    `gate_evidence_resolved: False`."""
    handoff_repo.seed_handoff(
        "2026-02-01-ac52-sentinel.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "PLACEHOLDER"',
    )

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-ac52-sentinel.md": {
                "covers_prose": True,
                "legs": [
                    {"leg_id": "leg1", "kind": "commit-ancestor", "observed": True},
                ],
            },
        }),
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    assert result["gates_cleared"] == [], result["gates_cleared"]
    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-ac52-sentinel")
    assert entry is not None, result["surfaced"]
    assert entry["gate_evidence_resolved"] is False, (
        "the scaffold sentinel short-circuits before rule 0 -- gate_evidence "
        "is never actually consulted, regardless of covers_prose"
    )
    assert (
        _deployment_state(handoff_repo, "2026-02-01-ac52-sentinel.md")
        == "awaiting_gate"
    )


# ---------------------------------------------------------------------------
# AC6/AC7 (C3 landing) -- prose-gate-outlived-structured-blockers contradiction
# surfacing, and the verdict ladder's terminal else-raise.
# ---------------------------------------------------------------------------


_CONTRADICTION = {
    "kind": "prose-gate-outlived-structured-blockers",
    "discharge_verb": "handoff.transition gate-recheck --cleared",
    "shipped_blocker_ids": ["b1", "b2"],
}


def test_contradiction_surfaces_distinguishably_from_plain_blocked_gate(
    handoff_repo, monkeypatch,
):
    """AC6: a `surface` verdict carrying `contradiction` must land in
    `surfaced[]` with the contradiction's kind and discharge verb readable
    from the entry an operator actually reads -- and a plain surfaced gate
    (no contradiction) must NOT carry the `contradiction` key at all, so the
    two are distinguishable by presence/absence, not by string sniffing."""
    handoff_repo.seed_handoff(
        "2026-02-01-contradiction-plain.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )
    handoff_repo.seed_handoff(
        "2026-02-01-contradiction-present.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )

    real_evaluate_gate = handoff_reconcile.evaluate_gate

    def _fake_evaluate_gate(handoff, *args, **kwargs):
        if handoff.get("id") == "2026-02-01-contradiction-present":
            return {"verdict": "surface", "evidence": ["stub"], "contradiction": dict(_CONTRADICTION)}
        return real_evaluate_gate(handoff, *args, **kwargs)

    monkeypatch.setattr(handoff_reconcile, "evaluate_gate", _fake_evaluate_gate)

    result = _run(_handler({}, handoff_repo.common_dir))

    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}

    plain_entry = surfaced_by_id.get("2026-02-01-contradiction-plain")
    assert plain_entry is not None, result["surfaced"]
    assert "contradiction" not in plain_entry

    entry = surfaced_by_id.get("2026-02-01-contradiction-present")
    assert entry is not None, result["surfaced"]
    assert entry["contradiction"] == _CONTRADICTION
    assert "prose-gate-outlived-structured-blockers" in entry["reason"]
    assert "handoff.transition gate-recheck --cleared" in entry["reason"]


def test_contradiction_composes_with_gate_evidence_reason_without_clobbering(
    handoff_repo, monkeypatch,
):
    """AC6 composition case: a handoff carrying BOTH a `gate_evidence` block
    (which sets its own 'still blocked' reason text) AND a contradiction on
    the evaluator's verdict must end up with a reason string carrying BOTH
    pieces of information -- the contradiction append must never clobber the
    gate_evidence branch's reason, and vice versa."""
    handoff_repo.seed_handoff(
        "2026-02-01-contradiction-with-evidence.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )

    monkeypatch.setattr(
        handoff_reconcile,
        "_read_gate_evidence_resolved",
        _fake_reader({
            "2026-02-01-contradiction-with-evidence.md": {
                "covers_prose": True,
                "legs": [
                    {"leg_id": "leg-human", "kind": "human", "reason": "needs a human look"},
                ],
            },
        }),
    )
    monkeypatch.setattr(
        handoff_reconcile,
        "evaluate_gate",
        lambda *args, **kwargs: {
            "verdict": "surface", "evidence": ["stub"], "contradiction": dict(_CONTRADICTION),
        },
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    surfaced_by_id = {s["handoff_id"]: s for s in result["surfaced"]}
    entry = surfaced_by_id.get("2026-02-01-contradiction-with-evidence")
    assert entry is not None, result["surfaced"]
    assert entry["gate_evidence_resolved"] is True
    assert entry["contradiction"] == _CONTRADICTION
    assert "gate_evidence resolved" in entry["reason"]
    assert "still blocked" in entry["reason"]
    assert "prose-gate-outlived-structured-blockers" in entry["reason"]
    assert "handoff.transition gate-recheck --cleared" in entry["reason"]
    assert (
        _deployment_state(handoff_repo, "2026-02-01-contradiction-with-evidence.md")
        == "awaiting_gate"
    )


def test_dry_run_report_renders_the_contradiction(handoff_repo, tmp_path, monkeypatch):
    """AC6: the durable dry-run report (`_build_dry_run_report`, threaded
    through `_handler`'s `report_path`) must render the contradiction --
    both the 'surface (contradiction)' proposed-verdict cell and the reason
    text carrying the discharge verb."""
    handoff_repo.seed_handoff(
        "2026-02-01-contradiction-report.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )
    monkeypatch.setattr(
        handoff_reconcile,
        "evaluate_gate",
        lambda *args, **kwargs: {
            "verdict": "surface", "evidence": ["stub"], "contradiction": dict(_CONTRADICTION),
        },
    )
    report_target = tmp_path / "dry-run.md"

    _run(_handler(
        {"report_path": str(report_target)}, repo_root=handoff_repo.common_dir,
    ))

    text = report_target.read_text(encoding="utf-8")
    assert "surface (contradiction)" in text
    assert "prose-gate-outlived-structured-blockers" in text
    assert "handoff.transition gate-recheck --cleared" in text


def test_unrecognized_gate_verdict_raises(handoff_repo, monkeypatch):
    """AC7: the verdict ladder's terminal else-raise fires when
    `evaluate_gate` returns a verdict outside its documented contract
    (`clear`, `narrow`, `surface`, `not-cleared`) -- a silent fall-through
    into the `not-cleared` no-op would swallow a future verdict-shape change
    without a trace."""
    handoff_repo.seed_handoff(
        "2026-02-01-unrecognized-verdict.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )
    monkeypatch.setattr(
        handoff_reconcile,
        "evaluate_gate",
        lambda *args, **kwargs: {"verdict": "bogus-verdict", "evidence": []},
    )

    try:
        _run(_handler({}, handoff_repo.common_dir))
    except ValueError as exc:
        assert "bogus-verdict" in str(exc)
        assert "2026-02-01-unrecognized-verdict" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unrecognized gate_eval verdict")


def test_not_cleared_verdict_is_still_the_benign_steady_state(handoff_repo, monkeypatch):
    """AC7 guard: C3 made the `not-cleared` branch explicit without changing
    its behaviour -- it must still take NO action and must NOT be surfaced,
    exactly as before this change."""
    handoff_repo.seed_handoff(
        "2026-02-01-not-cleared-steady.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "sibling repo ships the widgetforge feature"',
    )
    monkeypatch.setattr(
        handoff_reconcile,
        "evaluate_gate",
        lambda *args, **kwargs: {"verdict": "not-cleared", "evidence": []},
    )

    result = _run(_handler({}, handoff_repo.common_dir))

    surfaced_ids = {s["handoff_id"] for s in result["surfaced"]}
    gates_cleared_ids = {g["handoff_id"] for g in result["gates_cleared"]}
    reconciled_ids = {r["handoff_id"] for r in result["reconciled"]}
    assert "2026-02-01-not-cleared-steady" not in surfaced_ids
    assert "2026-02-01-not-cleared-steady" not in gates_cleared_ids
    assert "2026-02-01-not-cleared-steady" not in reconciled_ids
    assert (
        _deployment_state(handoff_repo, "2026-02-01-not-cleared-steady.md")
        == "awaiting_gate"
    )
