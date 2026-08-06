"""
coordinator_core.ops.tests.test_handoff_reconcile

Integration tests for the handoff.reconcile_open orchestrating op (C4):
enumerate the open handoff set, run C2 commit_reality + C3 gate_eval per handoff,
and either transition (auto-ship / gate-cascade-clear) or surface.

Import guard: coordinator_core.ops.handoff_reconcile MUST be imported at module
load time to fire @register_op("handoff.reconcile_open") and populate _REGISTRY —
otherwise a registry-completeness assertion passes vacuously over an empty entry
(lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage (AC5 fixture — 1 clear-ship, 1 clear-gate, 2 ambiguous):
  (a) op registered
  (b) dry_run=true (default) — every verdict computed, ZERO transitions performed
  (c) dry_run=false — exactly 2 transitions (1 ship_and_archive, 1 gate-cascade-clear
      flip) + exactly 2 surfaced (the two ambiguous handoffs), the not-cleared
      benign-steady-state handoff produces NEITHER a transition nor a surface entry

Spec backlink: docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C4
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("handoff.reconcile_open") as a side-effect.
# MUST precede any test function so the registry is populated before assertions.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_reconcile  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_reconcile import (
    _handler,
    _history_path,
    _resolve_dry_run,
    _save_surfaced_history,
)
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

_OP_NAME = "handoff.reconcile_open"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_reconcile @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo.root), capture_output=True, text=True, check=True,
    )


def _deployment_state(repo, name: str) -> str:
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field(split.fm_text, "deployment_state")


def _archived(repo, name: str) -> bool:
    return any((repo.root / "archive" / "handoffs").rglob(name)) if (
        repo.root / "archive" / "handoffs"
    ).is_dir() else False


def _write_enabled_policy(tmp_path: Path) -> str:
    """A grammar-valid policy fixture with auto_ship_enabled implicitly True
    (loaded-source default) AND dry_run: False, for tests that need to
    exercise the act-mode auto-ship transition path distinctly from the
    fail-closed default.

    D2(a): dry_run is now resolved from the LOADED POLICY, not a caller
    param — the caller no longer passes `dry_run: False` alongside this
    fixture (see the module's `_resolve_dry_run`); this fixture's own
    `dry_run: False` is what arms act-mode for every test that uses it.
    """
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


def _roadmap_extra(stub_id: str, blocked_by_yaml: str, gate_dependency: str = "") -> str:
    """kind: spinoff-roadmap requires roadmap_id/stub_id/wave/blocks/blocked_by
    (schema cross-field rule) — mirrors test_handoff_transition.py's helper."""
    lines = [
        "kind: spinoff-roadmap",
        'roadmap_id: "rdm-reconcile-test"',
        f'stub_id: "{stub_id}"',
        "wave: 1",
        "blocks: []",
        f"blocked_by: {blocked_by_yaml}",
    ]
    if gate_dependency:
        lines.append(f'gate_dependency: "{gate_dependency}"')
    return "\n".join(lines)


def _build_ac5_fixture(handoff_repo):
    """Seed the AC5 fixture: 1 clear-ship, 1 clear-gate, 2 ambiguous (surfaced),
    plus 1 not-cleared benign-steady-state handoff (neither transitioned nor
    surfaced — confirms the not-cleared exclusion holds under the full op).

    Returns the shipping-commit sha used for the clear-ship candidate.
    """
    # --- clear-ship candidate: a real committed deliverable file, subject token
    # match, reachable on HEAD, sole attribution (no other open handoff overlaps
    # its scope). ---
    deliverable = handoff_repo.root / "widgetforge_module.py"
    deliverable.write_text("# widgetforge implementation\n", encoding="utf-8")
    _git(handoff_repo, "add", "widgetforge_module.py")
    _git(handoff_repo, "commit", "-m", "ship widgetforge_module feature")
    ship_sha = _git(handoff_repo, "rev-parse", "HEAD").stdout.strip()

    handoff_repo.seed_handoff(
        "2026-01-01-widgetforge.md",
        "open",
        deployment_state="open",
        extra='title: "widgetforge_module rollout"\nscope:\n  - widgetforge_module.py',
    )

    # --- clear-gate candidate: awaiting_gate, structured blocked_by, sole blocker
    # already shipped. ---
    handoff_repo.seed_handoff(
        "blocker-shipped.md", "open", deployment_state="shipped",
        shipped_in="c" * 40,
        extra='stub_id: "stub-shipped"\nblocks: ["2026-01-01-gate-clear"]',
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-clear.md", "open",
        deployment_state="awaiting_gate",
        # No `gate_dependency` here (C4 prose-dominance): this candidate is
        # meant to exercise a genuinely-clearable STRUCTURED gate. Passing a
        # non-empty gate_dependency alongside blocked_by would now correctly
        # trip gate_eval's prose-dominance rule and surface instead of clear.
        extra=_roadmap_extra("gate-clear", "['stub-shipped']"),
    )

    # --- ambiguous #1: no candidate commit at all (C2 verdict=no-match) → surfaced. ---
    handoff_repo.seed_handoff(
        "2026-01-01-ambiguous-nomatch.md",
        "open",
        deployment_state="open",
        extra='title: "unrelated ambiguous work item"\nscope:\n  - nonexistent-path.py',
    )

    # --- ambiguous #2: awaiting_gate, structured blocked_by, blocker still open
    # (not-cleared) is NOT this one — this one has a PROSE gate with no witness,
    # which always surfaces regardless of clear/surface per example-doctrine-repo alignment reply #3. ---
    handoff_repo.seed_handoff(
        "2026-01-01-ambiguous-prose-gate.md",
        "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "some unresolved subsystem work"',
    )

    # --- not-cleared benign steady state: awaiting_gate, structured blocked_by,
    # sole blocker still open (neither shipped nor abandoned) → verdict
    # not-cleared → NO action, NOT surfaced. ---
    # status:consumed + deployment_state:awaiting_gate (NOT in_flight) keeps this
    # blocker OUT of reconcile_open's own widened open-set scan (C1's exact
    # archive-complement admits ONLY claimed+in_flight; claimed+awaiting_gate
    # stays excluded, already archive-swept) — it is a blocker referenced by id,
    # not itself a handoff under reconciliation. gate_eval resolves it purely via
    # stub_id lookup + its own deployment_state != "shipped", so awaiting_gate
    # (any non-terminal value) satisfies the same "still open" resolution the
    # original in_flight choice did, without now colliding with C1's widened
    # open-set enumeration (which would otherwise surface this blocker itself
    # as a spurious third entry once it too became independently enumerable).
    handoff_repo.seed_handoff(
        "blocker-open.md", "claimed", deployment_state="awaiting_gate",
        claimed_at="2026-01-01T00:00:00Z", claimed_by="s-blocker-open",
        extra='stub_id: "stub-open"\nblocks: ["2026-01-01-gate-not-cleared"]',
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-not-cleared.md", "open",
        deployment_state="awaiting_gate",
        # No `gate_dependency` here either (C4 prose-dominance) — this
        # candidate is meant to exercise the benign not-cleared STEADY
        # STATE (structured-only, sole blocker still open), not a prose
        # gate. With a non-empty gate_dependency alongside blocked_by,
        # prose-dominance would now correctly surface this instead of
        # leaving it not-cleared, contradicting the fixture's own intent.
        extra=_roadmap_extra("gate-not-cleared", "['stub-open']"),
    )

    return ship_sha


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY, (
        f"{_OP_NAME!r} must be registered; present ops: {sorted(_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# (b) dry_run=true (default) — zero transitions, verdicts still computed
# ---------------------------------------------------------------------------


def test_dry_run_default_true_performs_zero_transitions(handoff_repo):
    _build_ac5_fixture(handoff_repo)

    result = _run(_handler({}, handoff_repo.common_dir))

    assert result["exit_code"] == 0
    assert len(result["reconciled"]) == 1, result["reconciled"]
    assert result["reconciled"][0]["applied"] is False
    assert result["reconciled"][0]["dry_run"] is True

    assert len(result["gates_cleared"]) == 1, result["gates_cleared"]
    assert result["gates_cleared"][0]["applied"] is False
    assert result["gates_cleared"][0]["dry_run"] is True

    # Nothing on disk actually transitioned.
    assert _deployment_state(handoff_repo, "2026-01-01-widgetforge.md") == "open"
    assert _deployment_state(handoff_repo, "2026-01-01-gate-clear.md") == "awaiting_gate"
    assert not _archived(handoff_repo, "2026-01-01-widgetforge.md")

    # surfaced[] still populated (verdicts computed even though nothing transitions).
    surfaced_ids = {s["handoff_id"] for s in result["surfaced"]}
    assert "2026-01-01-ambiguous-nomatch" in surfaced_ids
    assert "2026-01-01-ambiguous-prose-gate" in surfaced_ids
    assert "2026-01-01-gate-not-cleared" not in surfaced_ids


def test_dry_run_explicit_true(handoff_repo):
    _build_ac5_fixture(handoff_repo)
    result = _run(_handler({"dry_run": True}, handoff_repo.common_dir))
    assert result["reconciled"][0]["applied"] is False
    assert result["gates_cleared"][0]["applied"] is False


# ---------------------------------------------------------------------------
# D2(a) — the loaded policy is the SOLE source of truth for dry_run; a
# disagreeing caller param requires a named+reasoned override or is refused.
# ---------------------------------------------------------------------------


def test_policy_dry_run_false_is_honoured_with_no_caller_param(handoff_repo, tmp_path):
    """The core D2(a) property: a policy declaring dry_run: False arms
    act-mode with NO caller dry_run param at all — dry_run is no longer a
    caller-params-only default."""
    ship_sha = _build_ac5_fixture(handoff_repo)
    policy_path = _write_enabled_policy(tmp_path)  # dry_run: False, auto_ship_enabled implicit True

    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["reconciled"][0]["applied"] is True, result["reconciled"]
    assert result["reconciled"][0]["dry_run"] is False
    assert _archived(handoff_repo, "2026-01-01-widgetforge.md")
    assert result["dry_run_override"] is None


def test_policy_dry_run_true_stays_dry_run_with_no_caller_param(handoff_repo, tmp_path):
    """The mirror case: an explicit, present, valid policy declaring
    dry_run: True (not merely absent) still performs zero transitions with
    no caller param — the SOLE-source-of-truth property holds in both
    directions, not just the arming one."""
    _build_ac5_fixture(handoff_repo)
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    policy_file.write_text(
        yaml.safe_dump({
            "three_signal": {},
            "mechanical_commit_denylist": [
                "pickup:", "reclaim(docs)", "session-init", "memo:", "handoff.transition",
            ],
            "cross_handoff_attribution": True,
            "dry_run": True,
        }),
        encoding="utf-8",
    )

    result = _run(_handler({"policy_path": str(policy_file)}, handoff_repo.common_dir))

    assert result["reconciled"][0]["applied"] is False
    assert result["reconciled"][0]["dry_run"] is True
    assert _deployment_state(handoff_repo, "2026-01-01-widgetforge.md") == "open"
    assert result["dry_run_override"] is None


def test_absent_policy_fails_closed_to_dry_run_true(handoff_repo):
    """FAIL-CLOSED MUST SURVIVE (D2(a)): no policy_path, no
    AUTO_RECONCILE_POLICY -> policy_loader's absent-branch conservative
    default -> dry_run resolves True with zero caller involvement."""
    _build_ac5_fixture(handoff_repo)

    result = _run(_handler({}, handoff_repo.common_dir))

    assert result["reconciled"][0]["dry_run"] is True
    assert result["reconciled"][0]["applied"] is False
    assert result["dry_run_override"] is None


def test_malformed_policy_fails_closed_to_dry_run_true(handoff_repo, tmp_path):
    """FAIL-CLOSED MUST SURVIVE (D2(a)): a policy file that exists but fails
    grammar-pin validation still resolves dry_run True — malformed is
    conservative, same as absent, per policy_loader's own fail-closed split."""
    _build_ac5_fixture(handoff_repo)
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    policy_file.write_text("not_a_valid_policy: true", encoding="utf-8")

    result = _run(_handler({"policy_path": str(policy_file)}, handoff_repo.common_dir))

    assert result["reconciled"][0]["dry_run"] is True
    assert result["reconciled"][0]["applied"] is False
    assert result["dry_run_override"] is None


def test_unnamed_dry_run_override_is_refused(handoff_repo, tmp_path):
    """A caller-supplied dry_run that DISAGREES with the policy value, with
    NO dry_run_override_reason, is refused — the policy value governs and
    the refusal is surfaced (not silently swallowed) via dry_run_override."""
    ship_sha = _build_ac5_fixture(handoff_repo)
    policy_path = _write_enabled_policy(tmp_path)  # policy declares dry_run: False

    # Caller asks for dry_run=True (the opposite of the policy) with no reason.
    result = _run(_handler({"dry_run": True, "policy_path": policy_path}, handoff_repo.common_dir))

    # Refused -> policy's dry_run (False) governs -> the transition still applies.
    assert result["reconciled"][0]["dry_run"] is False
    assert result["reconciled"][0]["applied"] is True
    assert _archived(handoff_repo, "2026-01-01-widgetforge.md")

    override = result["dry_run_override"]
    assert override is not None
    assert override["applied"] is False
    assert override["policy_dry_run"] is False
    assert override["requested_dry_run"] is True
    assert override["reason"] is None


def test_named_reasoned_dry_run_override_is_permitted_and_logged(handoff_repo, tmp_path, caplog):
    """A caller-supplied dry_run that disagrees with the policy value, PAIRED
    with a non-empty dry_run_override_reason, is applied and logged at
    WARNING — the explicitly-named escape hatch (mirrors
    handoff_stamp.py's _repair_archived_shipped_in_handler mandatory-reason
    pattern)."""
    _build_ac5_fixture(handoff_repo)
    # policy declares dry_run: True (via absent policy's fail-closed default);
    # caller overrides to False with a named reason.
    import logging
    caplog.set_level(logging.WARNING, logger="coordinator_core.ops.handoff_reconcile")

    result = _run(_handler(
        {"dry_run": False, "dry_run_override_reason": "manual test-time arming, see D2abc chunk"},
        handoff_repo.common_dir,
    ))

    # auto_ship_enabled is STILL False (absent policy) -- the override only
    # ever flips dry_run, never the independent AC10 auto_ship_enabled gate.
    assert result["reconciled"][0]["dry_run"] is False
    assert result["reconciled"][0]["applied"] is False
    assert result["reconciled"][0]["message"] == "auto_ship_enabled=false (fail-closed policy)"

    override = result["dry_run_override"]
    assert override is not None
    assert override["applied"] is True
    assert override["policy_dry_run"] is True
    assert override["requested_dry_run"] is False
    assert override["reason"] == "manual test-time arming, see D2abc chunk"
    assert any("dry_run OVERRIDE applied" in rec.message for rec in caplog.records)


def test_dry_run_override_with_empty_reason_string_is_refused(handoff_repo, tmp_path):
    """A whitespace-only / empty-string reason does not count as "named" —
    same refusal path as no reason at all."""
    _build_ac5_fixture(handoff_repo)

    result = _run(_handler(
        {"dry_run": False, "dry_run_override_reason": "   "},
        handoff_repo.common_dir,
    ))

    assert result["reconciled"][0]["dry_run"] is True
    override = result["dry_run_override"]
    assert override is not None
    assert override["applied"] is False


def test_required_policy_keys_all_have_a_runtime_consumer(handoff_repo, tmp_path):
    """Generalizable discharge-test companion (F14): every key
    policy_loader._REQUIRED_KEYS mandates the example-doctrine-repo-authored yaml declare must
    actually be READ by some consumer at runtime — a schema that requires a
    field nobody reads is the exact D2abc defect (dry_run required but
    unconsumed until this chunk). Drives the policy through its real
    consumers with a tracking-dict wrapper rather than a hand-maintained grep
    pattern, so a future required key without a matching grep gets caught
    automatically.
    """
    from coordinator_core.reconcile.commit_reality import evaluate_commit_reality
    from coordinator_core.reconcile.policy_loader import _REQUIRED_KEYS

    class _TrackingPolicy(dict):
        def __init__(self, data):
            super().__init__(data)
            self.accessed: set = set()

        def get(self, key, default=None):
            self.accessed.add(key)
            return dict.get(self, key, default)

        def __getitem__(self, key):
            self.accessed.add(key)
            return dict.__getitem__(self, key)

    _build_ac5_fixture(handoff_repo)

    base_policy = {
        "three_signal": {},
        "mechanical_commit_denylist": [
            "pickup:", "reclaim(docs)", "session-init", "memo:", "handoff.transition",
        ],
        "cross_handoff_attribution": True,
        "dry_run": True,
    }
    tracking = _TrackingPolicy(base_policy)

    # Drive the two documented per-handoff consumers (commit_reality.py) plus
    # this op's own dry_run resolution (handoff_reconcile.py) with the SAME
    # tracking policy object -- these are policy_loader's real, live consumers
    # (see its module docstring's "Consumers:" list), not a synthetic stand-in.
    handoff = {"id": "tracking-probe", "scope": ["widgetforge_module.py"], "title": "probe"}
    evaluate_commit_reality(handoff, handoff_repo.root, tracking, [])
    _resolve_dry_run(tracking, {})

    missing = set(_REQUIRED_KEYS) - tracking.accessed
    assert not missing, (
        f"policy_loader._REQUIRED_KEYS keys with no runtime consumer: {sorted(missing)} "
        "-- a required-but-unread key is the exact D2abc defect this test guards against"
    )


# ---------------------------------------------------------------------------
# (c) dry_run=false — exactly 2 transitions, exactly 2 surfaced, not-cleared
#     produces neither
# ---------------------------------------------------------------------------


def test_act_mode_two_transitions_two_surfaced(handoff_repo, tmp_path):
    """Exercises the act-mode transition path with an explicit grammar-valid
    policy (auto_ship_enabled=True) injected via policy_path — distinct from
    test_absent_policy_fail_closed_blocks_auto_ship_even_in_act_mode, which
    covers the AC10 fail-closed default (no policy_path => auto_ship_enabled
    defaults to False)."""
    ship_sha = _build_ac5_fixture(handoff_repo)
    policy_path = _write_enabled_policy(tmp_path)

    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result

    # Exactly 1 auto-ship transition.
    assert len(result["reconciled"]) == 1, result["reconciled"]
    ship_entry = result["reconciled"][0]
    assert ship_entry["handoff_id"] == "2026-01-01-widgetforge"
    assert ship_entry["applied"] is True, ship_entry
    assert ship_entry["candidate_sha"] == ship_sha

    # Exactly 1 gate-cascade-clear transition.
    assert len(result["gates_cleared"]) == 1, result["gates_cleared"]
    gate_entry = result["gates_cleared"][0]
    assert gate_entry["handoff_id"] == "2026-01-01-gate-clear"
    assert gate_entry["applied"] is True, gate_entry
    assert gate_entry["verdict"] == "clear"

    # Exactly 2 surfaced (the two ambiguous handoffs) — not-cleared excluded.
    assert len(result["surfaced"]) == 2, result["surfaced"]
    surfaced_ids = {s["handoff_id"] for s in result["surfaced"]}
    assert surfaced_ids == {
        "2026-01-01-ambiguous-nomatch",
        "2026-01-01-ambiguous-prose-gate",
    }

    # On-disk verification: ship transitioned + archived.
    assert _archived(handoff_repo, "2026-01-01-widgetforge.md")

    # On-disk verification: gate flipped to ready_to_fire, gate_dependency stripped.
    assert _deployment_state(handoff_repo, "2026-01-01-gate-clear.md") == "ready_to_fire"
    gate_clear_fm = split_frontmatter(
        handoff_repo.read_text("2026-01-01-gate-clear.md")
    ).fm_text
    assert read_fm_field(gate_clear_fm, "gate_dependency") is None

    # not-cleared benign steady state: untouched, not surfaced.
    assert _deployment_state(handoff_repo, "2026-01-01-gate-not-cleared.md") == "awaiting_gate"
    assert "2026-01-01-gate-not-cleared" not in surfaced_ids


# ---------------------------------------------------------------------------
# AC10 fail-closed gate — auto_ship_enabled=False (absent/malformed policy)
# must suppress the auto-ship transition even with dry_run=false.
# ---------------------------------------------------------------------------


def test_absent_policy_fail_closed_blocks_auto_ship_even_in_act_mode(handoff_repo):
    """No `AUTO_RECONCILE_POLICY` / policy_path is set by this fixture, so
    load_policy() resolves to the absent-policy conservative default
    (auto_ship_enabled=False, dry_run=True). A handoff whose commit_reality
    verdict would otherwise clear the three-signal bar must produce ZERO
    ship_and_archive invocations even when the caller REQUESTS act-mode via
    an unnamed `dry_run=False` override — this is the AC10 fail-closed
    safety property (EM-verified P0 upgrade of Slice-A Finding 2), now
    doubly enforced post-D2(a): the unnamed override is refused (no
    `dry_run_override_reason`), so dry_run stays True on the policy's own
    say-so, AND auto_ship_enabled independently stays False regardless."""
    _build_ac5_fixture(handoff_repo)

    result = _run(_handler({"dry_run": False}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert len(result["reconciled"]) == 1, result["reconciled"]
    ship_entry = result["reconciled"][0]
    assert ship_entry["handoff_id"] == "2026-01-01-widgetforge"
    assert ship_entry["applied"] is False, ship_entry
    assert ship_entry["message"] == "auto_ship_enabled=false (fail-closed policy)"

    # Nothing on disk actually transitioned or archived.
    assert _deployment_state(handoff_repo, "2026-01-01-widgetforge.md") == "open"
    assert not _archived(handoff_repo, "2026-01-01-widgetforge.md")


# ---------------------------------------------------------------------------
# C3 (reconcile-open-consumed-in-flight-dead-zone) — the stranded-baton class:
# claimed+in_flight, the exact dead zone C1 widens _is_open to admit.
# ---------------------------------------------------------------------------


def _write_plan(handoff_repo, rel_path: str, status: str) -> None:
    """Write a minimal docs/plans/*.md fixture with a frontmatter `status:` field —
    the linked-plan corroboration signal (C2 sub-signal ii)."""
    full = handoff_repo.root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\nstatus: {status}\n---\n\n# Plan\n", encoding="utf-8")
    handoff_repo._git("add", rel_path)
    handoff_repo._git("commit", "-m", f"memo: seed plan {rel_path} (mechanical)")


def test_stranded_consumed_in_flight_shipped_is_auto_shipped(handoff_repo, tmp_path):
    """The motivating dead-zone shape: claimed+in_flight, real shipping commit in
    scope, deliverable present, SHA on HEAD, touched paths overlap own scope. The
    widened _is_open (C1) must enumerate it; act-mode reconcile auto-ships+archives."""
    deliverable = handoff_repo.root / "stranded_baton_module.py"
    deliverable.write_text("# stranded baton implementation\n", encoding="utf-8")
    handoff_repo._git("add", "stranded_baton_module.py")
    handoff_repo._git("commit", "-m", "ship stranded_baton_module feature")
    ship_sha = _git(handoff_repo, "rev-parse", "HEAD").stdout.strip()

    handoff_repo.seed_handoff(
        "2026-01-05-stranded.md", "claimed",
        deployment_state="in_flight",
        claimed_at="2026-01-05T00:00:00Z", claimed_by="s-stranded",
        extra='title: "stranded_baton_module rollout"\nscope:\n  - stranded_baton_module.py',
    )

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    ship_entries = [r for r in result["reconciled"] if r["handoff_id"] == "2026-01-05-stranded"]
    assert len(ship_entries) == 1, result["reconciled"]
    assert ship_entries[0]["applied"] is True, ship_entries[0]
    assert ship_entries[0]["candidate_sha"] == ship_sha
    assert _archived(handoff_repo, "2026-01-05-stranded.md")


def test_stranded_consumed_in_flight_unshipped_is_surfaced_not_shipped(handoff_repo, tmp_path):
    """Sibling of the above: claimed+in_flight, linked plan status==approved, NO
    shipping commit anywhere — guards the emit-leg/commit-closure shape (two
    genuinely in-progress consumed batons that must surface, never auto-ship)."""
    _write_plan(handoff_repo, "docs/plans/2026-01-06-unshipped-work.md", "approved")

    handoff_repo.seed_handoff(
        "2026-01-06-unshipped.md", "claimed",
        deployment_state="in_flight",
        claimed_at="2026-01-06T00:00:00Z", claimed_by="s-unshipped",
        extra=(
            'title: "unshipped in-progress work"\n'
            "scope:\n  - docs/plans/2026-01-06-unshipped-work.md"
        ),
    )

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    ship_ids = {r["handoff_id"] for r in result["reconciled"]}
    assert "2026-01-06-unshipped" not in ship_ids
    surfaced_ids = {s["handoff_id"] for s in result["surfaced"]}
    assert "2026-01-06-unshipped" in surfaced_ids
    assert _deployment_state(handoff_repo, "2026-01-06-unshipped.md") == "in_flight"
    assert not _archived(handoff_repo, "2026-01-06-unshipped.md")


def test_stranded_shipped_in_not_reachable_surfaces_not_ships(handoff_repo, tmp_path):
    """Blind-trust guard: a frontmatter shipped_in SHA that is NOT git-reachable on
    HEAD (rebased away / never merged) must surface, never auto-ship on the stamp
    alone."""
    deliverable = handoff_repo.root / "unreachable_baton_module.py"
    deliverable.write_text("# present on disk\n", encoding="utf-8")
    handoff_repo._git("add", "unreachable_baton_module.py")
    handoff_repo._git("commit", "-m", "memo: seed unreachable baton module (mechanical)")

    handoff_repo.seed_handoff(
        "2026-01-07-unreachable.md", "claimed",
        deployment_state="in_flight",
        claimed_at="2026-01-07T00:00:00Z", claimed_by="s-unreachable",
        shipped_in="f" * 40,
        extra='title: "unreachable baton module"\nscope:\n  - unreachable_baton_module.py',
    )

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    ship_ids = {r["handoff_id"] for r in result["reconciled"]}
    assert "2026-01-07-unreachable" not in ship_ids
    surfaced_ids = {s["handoff_id"] for s in result["surfaced"]}
    assert "2026-01-07-unreachable" in surfaced_ids
    assert _deployment_state(handoff_repo, "2026-01-07-unreachable.md") == "in_flight"
    assert not _archived(handoff_repo, "2026-01-07-unreachable.md")


def test_shipped_in_reachable_but_disjoint_scope_surfaces_not_ships(handoff_repo, tmp_path):
    """F1/F3a load-bearing negative: shipped_in SHA reachable on HEAD AND
    deliverable-present, but its touched paths are DISJOINT from this handoff's
    OWN scope (e.g. copied from a sibling handoff) — must surface naming the
    disjoint scope, never ship on reachability+presence alone (self-scope-overlap
    gate, distinct from the not-reachable fixture above)."""
    own_scope_file = handoff_repo.root / "self_scope_module.py"
    own_scope_file.write_text("# own scope, present on disk\n", encoding="utf-8")
    handoff_repo._git("add", "self_scope_module.py")
    handoff_repo._git("commit", "-m", "memo: seed self scope module (mechanical)")

    unrelated_file = handoff_repo.root / "unrelated_sibling_module.py"
    unrelated_file.write_text("# unrelated sibling work\n", encoding="utf-8")
    handoff_repo._git("add", "unrelated_sibling_module.py")
    handoff_repo._git("commit", "-m", "ship unrelated_sibling_module feature")
    unrelated_sha = _git(handoff_repo, "rev-parse", "HEAD").stdout.strip()

    handoff_repo.seed_handoff(
        "2026-01-08-disjoint-scope.md", "claimed",
        deployment_state="in_flight",
        claimed_at="2026-01-08T00:00:00Z", claimed_by="s-disjoint",
        shipped_in=unrelated_sha,
        extra='title: "self scope module"\nscope:\n  - self_scope_module.py',
    )

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    ship_ids = {r["handoff_id"] for r in result["reconciled"]}
    assert "2026-01-08-disjoint-scope" not in ship_ids
    surfaced = {s["handoff_id"]: s for s in result["surfaced"]}
    assert "2026-01-08-disjoint-scope" in surfaced
    evidence = surfaced["2026-01-08-disjoint-scope"]["evidence"]
    # commit_reality.py (2083a491) now discriminates on shipped_in_kind BEFORE any
    # git dereferencing: an untagged shipped_in fails closed to partial/surface ahead
    # of the scope-overlap check, so the untagged-tag reason short-circuits and the
    # scope-overlap diagnostic below never runs for this fixture (it seeds shipped_in
    # with no shipped_in_kind). The not-shipped/surfaced outcome this test guards is
    # unchanged; only the human-readable reason precedes it now.
    assert any(
        "carries no shipped_in_kind tag" in e for e in evidence
    ), evidence
    assert not _archived(handoff_repo, "2026-01-08-disjoint-scope.md")


def test_negative_consumed_awaiting_gate_and_ready_to_fire_not_enumerated(handoff_repo):
    """F1 regression guard: the C1 widened _is_open must stay the EXACT
    archive-complement — claimed+awaiting_gate and claimed+ready_to_fire are
    ALREADY archive-swept (Branch A terminal) and must NOT be admitted into the
    open set, even though they are technically 'consumed' + not 'in_flight'."""
    handoff_repo.seed_handoff(
        "2026-01-09-consumed-awaiting-gate.md", "claimed",
        deployment_state="awaiting_gate",
        claimed_at="2026-01-09T00:00:00Z", claimed_by="s-awaiting",
    )
    handoff_repo.seed_handoff(
        "2026-01-09-consumed-ready-to-fire.md", "claimed",
        deployment_state="ready_to_fire",
        claimed_at="2026-01-09T00:00:00Z", claimed_by="s-ready",
    )

    from coordinator_core.ops.handoff_reconcile import _collect_open_handoffs

    open_ids = {h["id"] for h in _collect_open_handoffs(handoff_repo.root)}
    assert "2026-01-09-consumed-awaiting-gate" not in open_ids
    assert "2026-01-09-consumed-ready-to-fire" not in open_ids

    # Full-op confirmation: neither handoff is touched, transitioned, or surfaced.
    result = _run(_handler({"dry_run": True}, handoff_repo.common_dir))
    all_ids = (
        {r["handoff_id"] for r in result["reconciled"]}
        | {g["handoff_id"] for g in result["gates_cleared"]}
        | {s["handoff_id"] for s in result["surfaced"]}
    )
    assert "2026-01-09-consumed-awaiting-gate" not in all_ids
    assert "2026-01-09-consumed-ready-to-fire" not in all_ids


def test_ac4_cross_handoff_attribution_ambiguous_consumed_predecessor(handoff_repo, tmp_path):
    """AC4: the cross-handoff attribution guard still demotes to `surface` when a
    live successor shares scope overlap with a consumed predecessor's shipping
    commit — proven with a claimed+in_flight predecessor (the new dead-zone
    shape) rather than the pre-existing active/active AC4 shape."""
    shared_file = handoff_repo.root / "shared_attribution_module.py"
    shared_file.write_text("# shared attribution work\n", encoding="utf-8")
    handoff_repo._git("add", "shared_attribution_module.py")
    handoff_repo._git("commit", "-m", "ship shared_attribution_module feature")
    shared_sha = _git(handoff_repo, "rev-parse", "HEAD").stdout.strip()

    handoff_repo.seed_handoff(
        "2026-01-10-attribution-predecessor.md", "claimed",
        deployment_state="in_flight",
        claimed_at="2026-01-10T00:00:00Z", claimed_by="s-attribution-pred",
        extra=(
            'title: "shared_attribution_module predecessor"\n'
            "scope:\n  - shared_attribution_module.py"
        ),
    )
    handoff_repo.seed_handoff(
        "2026-01-10-attribution-successor.md", "open",
        deployment_state="open",
        extra=(
            'title: "shared_attribution_module successor"\n'
            "scope:\n  - shared_attribution_module.py"
        ),
    )

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    ship_ids = {r["handoff_id"] for r in result["reconciled"]}
    assert "2026-01-10-attribution-predecessor" not in ship_ids
    assert "2026-01-10-attribution-successor" not in ship_ids
    surfaced_ids = {s["handoff_id"] for s in result["surfaced"]}
    assert "2026-01-10-attribution-predecessor" in surfaced_ids
    assert "2026-01-10-attribution-successor" in surfaced_ids
    assert _deployment_state(handoff_repo, "2026-01-10-attribution-predecessor.md") == "in_flight"
    assert _deployment_state(handoff_repo, "2026-01-10-attribution-successor.md") == "open"


# ---------------------------------------------------------------------------
# C6 (AC8) — pinned predecessor chain-walk. A ready_to_fire successor whose OWN
# work shipped, walked upward via predecessor/origin_handoff, must archive a
# pinned claimed+in_flight ancestor ONLY when independently verified (never on
# chain topology alone).
# ---------------------------------------------------------------------------


def _seed_chain_ancestor(handoff_repo, name: str, scope_files, title: str):
    scope_yaml = "\n".join(f"  - {f}" for f in scope_files)
    return handoff_repo.seed_handoff(
        name, "claimed",
        deployment_state="in_flight",
        claimed_at="2026-01-11T00:00:00Z", claimed_by=f"s-{Path(name).stem}",
        extra=f'title: "{title}"\nscope:\n{scope_yaml}',
    )


def _seed_chain_successor(handoff_repo, name: str, scope_files, title: str, predecessor_name: str):
    scope_yaml = "\n".join(f"  - {f}" for f in scope_files)
    return handoff_repo.seed_handoff(
        name, "open",
        deployment_state="ready_to_fire",
        predecessor=predecessor_name,
        extra=f'title: "{title}"\nscope:\n{scope_yaml}',
    )


def test_ac8_chain_full_archive_ancestor_scope_fully_subsumed(handoff_repo, tmp_path):
    """AC8 positive: a ready_to_fire successor whose shipping commit's touched
    paths FULLY SUBSUME a pinned claimed+in_flight predecessor's own scope —
    chain-walk archives BOTH, with the successor's SHA stamped into the
    archived predecessor's shipped_in (gate (a))."""
    ancestor_path = _seed_chain_ancestor(
        handoff_repo, "2026-01-11-chain-ancestor.md",
        ["chain_ancestor_deliverable.py"],
        "chain ancestor original scope",
    )
    _seed_chain_successor(
        handoff_repo, "2026-01-11-chain-successor.md",
        ["chain_successor_deliverable.py"],
        "chain successor continuation",
        "2026-01-11-chain-ancestor.md",
    )

    # ONE commit lands both the successor's own deliverable AND the ancestor's
    # never-finished deliverable — the shape of a continuation session that
    # picks up a stranded baton's scope and finishes it under a new baton.
    (handoff_repo.root / "chain_successor_deliverable.py").write_text(
        "# chain successor deliverable\n", encoding="utf-8"
    )
    (handoff_repo.root / "chain_ancestor_deliverable.py").write_text(
        "# chain ancestor deliverable, finished by the successor\n", encoding="utf-8"
    )
    handoff_repo._git("add", "chain_successor_deliverable.py", "chain_ancestor_deliverable.py")
    handoff_repo._git("commit", "-m", "ship chain_successor_deliverable feature")
    successor_sha = _git(handoff_repo, "rev-parse", "HEAD").stdout.strip()

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result

    assert _archived(handoff_repo, "2026-01-11-chain-successor.md"), result
    assert _archived(handoff_repo, "2026-01-11-chain-ancestor.md"), result

    # The archived ancestor's shipped_in must carry the SUCCESSOR's sha (gate a),
    # never a bare inference — read it back from the archive tree.
    archive_dir = handoff_repo.root / "archive" / "handoffs"
    ancestor_archived = next(archive_dir.rglob("2026-01-11-chain-ancestor.md"))
    ancestor_fm = split_frontmatter(ancestor_archived.read_text(encoding="utf-8")).fm_text
    assert read_fm_field(ancestor_fm, "shipped_in") == successor_sha
    # DR-096: gate (a) stamps the discriminant as "successor", not "ship-commit" —
    # the ancestor's shipped_in is the SUCCESSOR's sha, not its own.
    assert read_fm_field(ancestor_fm, "shipped_in_kind") == "successor"


def test_ac8_chain_partial_scope_survives_ancestor_not_subsumed(handoff_repo, tmp_path):
    """AC8 negative (load-bearing, F2/F3b): a pinned predecessor with scope
    {A,B}; its successor ships only {B} (touched paths do not subsume A) —
    predecessor is left untouched (surface), NOT archived. The partial-scope-
    spinoff false-archive vector."""
    _seed_chain_ancestor(
        handoff_repo, "2026-01-12-chain-ancestor-partial.md",
        ["partial_scope_a.py", "partial_scope_b.py"],
        "partial scope ancestor covering A and B",
    )
    _seed_chain_successor(
        handoff_repo, "2026-01-12-chain-successor-partial.md",
        ["partial_successor_deliverable.py"],
        "partial successor continuation",
        "2026-01-12-chain-ancestor-partial.md",
    )

    # The successor's shipping commit finishes its OWN deliverable and ONLY
    # scope_b of the ancestor — scope_a is never touched.
    (handoff_repo.root / "partial_successor_deliverable.py").write_text(
        "# partial successor deliverable\n", encoding="utf-8"
    )
    (handoff_repo.root / "partial_scope_b.py").write_text(
        "# partial scope b, finished by the successor\n", encoding="utf-8"
    )
    handoff_repo._git("add", "partial_successor_deliverable.py", "partial_scope_b.py")
    handoff_repo._git("commit", "-m", "ship partial_successor_deliverable feature")

    # scope_a is authored separately, never referenced by the shipping commit
    # (present on disk but never touched by the successor's commit).
    (handoff_repo.root / "partial_scope_a.py").write_text(
        "# scope a, never finished\n", encoding="utf-8"
    )
    handoff_repo._git("add", "partial_scope_a.py")
    handoff_repo._git("commit", "-m", "memo: seed partial scope a (mechanical, unfinished)")

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert _archived(handoff_repo, "2026-01-12-chain-successor-partial.md"), result
    # The load-bearing assertion: the ancestor with un-subsumed scope_a is NOT archived.
    assert not _archived(handoff_repo, "2026-01-12-chain-ancestor-partial.md"), result
    assert _deployment_state(handoff_repo, "2026-01-12-chain-ancestor-partial.md") == "in_flight"


def test_ac8_chain_unshipped_successor_leaves_both_untouched(handoff_repo, tmp_path):
    """AC8 negative (unshipped): a ready_to_fire successor with NO ship evidence
    (C2 surface/no-match) leaves both it AND its pinned predecessor untouched —
    the 'genuinely pending' case. Also mirrors the 7 live roadmap-sat-* nodes
    surviving a reconcile pass unchanged (multiple independent pending chains)."""
    for i in range(1, 8):
        _seed_chain_ancestor(
            handoff_repo, f"2026-01-13-roadmap-sat-{i:02d}-ancestor.md",
            [f"roadmap_sat_{i:02d}_scope.py"],
            f"roadmap-sat-{i:02d} ancestor pending work",
        )
        _seed_chain_successor(
            handoff_repo, f"2026-01-13-roadmap-sat-{i:02d}-successor.md",
            [f"roadmap_sat_{i:02d}_successor_scope.py"],
            f"roadmap-sat-{i:02d} successor pending work",
            f"2026-01-13-roadmap-sat-{i:02d}-ancestor.md",
        )
        # NOTE: no deliverable files, no commits — genuinely nothing has shipped.

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    ship_ids = {r["handoff_id"] for r in result["reconciled"]}
    for i in range(1, 8):
        ancestor_id = f"2026-01-13-roadmap-sat-{i:02d}-ancestor"
        successor_id = f"2026-01-13-roadmap-sat-{i:02d}-successor"
        assert ancestor_id not in ship_ids
        assert successor_id not in ship_ids
        assert not _archived(handoff_repo, f"{ancestor_id}.md")
        assert not _archived(handoff_repo, f"{successor_id}.md")
        assert _deployment_state(handoff_repo, f"{ancestor_id}.md") == "in_flight"
        assert _deployment_state(handoff_repo, f"{successor_id}.md") == "ready_to_fire"


def test_ac8_chain_r3_live_claim_blocks_archive_even_when_scope_subsumed(
    handoff_repo, tmp_path, monkeypatch
):
    """AC8 negative (load-bearing, R3 — live-claim): a pinned predecessor whose
    scope IS fully subsumed by the shipped successor's touched paths (would
    otherwise pass the scope-subsumption gate) but holds a live claim (is the
    live claimed_by of a running session) → chain-walk does NOT archive it,
    proving the per-ancestor liveness gate is applied independently of and in
    addition to scope subsumption."""
    live_sid = "s-live-claim-ancestor"

    _seed_chain_ancestor(
        handoff_repo, "2026-01-14-chain-ancestor-liveclaim.md",
        ["liveclaim_ancestor_deliverable.py"],
        "live claim ancestor scope",
    )
    # Override claimed_by to a session id we will report as LIVE below.
    fm_path = handoff_repo.root / "state" / "handoffs" / "2026-01-14-chain-ancestor-liveclaim.md"
    text = fm_path.read_text(encoding="utf-8")
    assert "claimed_by:" in text
    text = re.sub(r"claimed_by:.*", f"claimed_by: {live_sid}", text, count=1)
    fm_path.write_text(text, encoding="utf-8")
    handoff_repo._git("add", "state/handoffs/2026-01-14-chain-ancestor-liveclaim.md")
    handoff_repo._git("commit", "-m", "memo: stamp live claimed_by on ancestor (mechanical)")

    _seed_chain_successor(
        handoff_repo, "2026-01-14-chain-successor-liveclaim.md",
        ["liveclaim_successor_deliverable.py"],
        "live claim successor continuation",
        "2026-01-14-chain-ancestor-liveclaim.md",
    )

    (handoff_repo.root / "liveclaim_successor_deliverable.py").write_text(
        "# liveclaim successor deliverable\n", encoding="utf-8"
    )
    (handoff_repo.root / "liveclaim_ancestor_deliverable.py").write_text(
        "# liveclaim ancestor deliverable, finished by the successor\n", encoding="utf-8"
    )
    handoff_repo._git(
        "add", "liveclaim_successor_deliverable.py", "liveclaim_ancestor_deliverable.py"
    )
    handoff_repo._git("commit", "-m", "ship liveclaim_successor_deliverable feature")

    # The ancestor's scope IS fully subsumed by this shipping commit (gate (a)
    # would otherwise clear) — the ONLY thing standing between this ancestor
    # and a false archive is the liveness gate.
    monkeypatch.setattr(
        "coordinator_core.ops.handoff_reconcile.resolve_live_session_ids",
        lambda: frozenset({live_sid}),
    )

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert _archived(handoff_repo, "2026-01-14-chain-successor-liveclaim.md"), result
    # The load-bearing assertion: the live-claimed ancestor is NOT archived.
    assert not _archived(handoff_repo, "2026-01-14-chain-ancestor-liveclaim.md"), result
    assert (
        _deployment_state(handoff_repo, "2026-01-14-chain-ancestor-liveclaim.md")
        == "in_flight"
    )
    chain_entries = [
        r for r in result["reconciled"]
        if r.get("handoff_id") == "2026-01-14-chain-ancestor-liveclaim"
    ]
    assert len(chain_entries) == 1, result["reconciled"]
    assert chain_entries[0]["applied"] is False
    assert "liveness gate blocked" in chain_entries[0]["message"]
    assert "is live" in chain_entries[0]["message"] or "live" in chain_entries[0]["message"]


def test_ac8_chain_grandparent_liveness_blocked_by_open_middle_child(handoff_repo, tmp_path):
    """AC8 negative (load-bearing, code-review Finding 2 — P2): a three-generation
    chain grandparent G <- middle ancestor A <- shipped successor S. G's OWN
    scope IS fully subsumed by S's shipping commit (G's evidence gate clears
    independently of chain topology). A is genuinely still open (its own scope
    is never touched, so A's evidence gate correctly fails, leaving A
    untouched) and A's `predecessor` names G.

    Before the 2026-07-17 archival.py fix (bug-backlog
    2026-07-17-archival-reverse-membership-ignores-deployment-state.yaml),
    archival.py's reverse_membership could not see A as a live child of G — A's
    status=="claimed" unconditionally excluded it from archival.py's live-
    children set, with no reference to A's deployment_state=="in_flight" (C1's
    widened-open carve-out); only the F2 hardening in THIS module (an
    independent scan of its own open set for a declared predecessor/
    origin_handoff edge naming G) caught it, surfacing "live child by declared
    lineage". With the archival.py fix landed, reverse_membership itself now
    correctly counts A as live (a consumed child with deployment_state==
    "in_flight" is retained), so this scenario is now caught at the PRIMARY
    liveness gate ("has live children") — the F2 declared-lineage scan is a
    redundant (still-correct) defense-in-depth backstop for this exact case,
    not the sole detector. Asserts G is NOT archived (blocked by A's liveness)
    while S ships and A is left untouched on its own evidence gate."""
    _seed_chain_ancestor(
        handoff_repo, "2026-01-16-chain-grandparent.md",
        ["grandparent_scope.py"],
        "grandparent original scope",
    )
    # Seeded directly (not via _seed_chain_ancestor) so its `predecessor` names
    # the grandparent from the start — the declared lineage edge F2's fix
    # scans for.
    handoff_repo.seed_handoff(
        "2026-01-16-chain-middle.md", "claimed",
        deployment_state="in_flight",
        claimed_at="2026-01-11T00:00:00Z", claimed_by="s-2026-01-16-chain-middle",
        predecessor="2026-01-16-chain-grandparent.md",
        extra='title: "middle ancestor genuinely still open"\nscope:\n  - middle_scope.py',
    )

    _seed_chain_successor(
        handoff_repo, "2026-01-16-chain-successor.md",
        ["grandchild_successor_deliverable.py"],
        "successor continuation",
        "2026-01-16-chain-middle.md",
    )

    # The shipping commit finishes the successor's OWN deliverable AND the
    # grandparent's scope (grandparent's evidence gate clears independently)
    # but never touches the middle ancestor's scope (middle's own evidence
    # gate correctly fails).
    (handoff_repo.root / "grandchild_successor_deliverable.py").write_text(
        "# successor deliverable\n", encoding="utf-8"
    )
    (handoff_repo.root / "grandparent_scope.py").write_text(
        "# grandparent scope, finished by the successor's commit\n", encoding="utf-8"
    )
    handoff_repo._git(
        "add", "grandchild_successor_deliverable.py", "grandparent_scope.py",
    )
    handoff_repo._git("commit", "-m", "ship grandchild_successor_deliverable feature")

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert _archived(handoff_repo, "2026-01-16-chain-successor.md"), result
    # The load-bearing assertion: the grandparent is NOT archived, because its
    # immediate middle child is still genuinely open and names it as
    # predecessor — even though archival.py's reverse_membership alone cannot
    # see that liveness.
    assert not _archived(handoff_repo, "2026-01-16-chain-grandparent.md"), result
    assert not _archived(handoff_repo, "2026-01-16-chain-middle.md"), result
    assert _deployment_state(handoff_repo, "2026-01-16-chain-grandparent.md") == "in_flight"
    assert _deployment_state(handoff_repo, "2026-01-16-chain-middle.md") == "in_flight"

    grandparent_entries = [
        r for r in result["reconciled"]
        if r.get("handoff_id") == "2026-01-16-chain-grandparent"
    ]
    assert len(grandparent_entries) == 1, result["reconciled"]
    assert grandparent_entries[0]["applied"] is False
    assert "liveness gate blocked" in grandparent_entries[0]["message"]
    # 2026-07-17: with the archival.py deployment_state-aware terminal-predicate
    # fix landed, reverse_membership itself now correctly counts the middle
    # ancestor as live, so the PRIMARY "has live children" gate fires here
    # rather than the F2 declared-lineage fallback scan. This message text
    # documents the fix having moved detection upstream to its root cause —
    # it does NOT mean the F2 declared-lineage hardening is dead code (it
    # remains the backstop for cases archival.py's fail-closed-on-indeterminate
    # frontmatter still can't see).
    assert "has live children" in grandparent_entries[0]["message"]


# ---------------------------------------------------------------------------
# AC9 — cadence-idempotency: safe to invoke repeatedly, no-op on already-
# archived nodes or an empty open set.
# ---------------------------------------------------------------------------


def test_ac9_idempotent_reinvoke_is_clean_noop(handoff_repo, tmp_path):
    """Run act-mode reconcile twice over the same tree: second run must be a
    clean no-op (already-archived nodes are not re-shipped, no error)."""
    deliverable = handoff_repo.root / "idempotent_module.py"
    deliverable.write_text("# idempotent implementation\n", encoding="utf-8")
    handoff_repo._git("add", "idempotent_module.py")
    handoff_repo._git("commit", "-m", "ship idempotent_module feature")

    handoff_repo.seed_handoff(
        "2026-01-15-idempotent.md", "claimed",
        deployment_state="in_flight",
        claimed_at="2026-01-15T00:00:00Z", claimed_by="s-idempotent",
        extra='title: "idempotent_module rollout"\nscope:\n  - idempotent_module.py',
    )

    policy_path = _write_enabled_policy(tmp_path)

    first = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))
    assert first["exit_code"] == 0, first
    assert _archived(handoff_repo, "2026-01-15-idempotent.md")

    second = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))
    assert second["exit_code"] == 0, second
    # Already archived → no longer part of the open set → no reconciled/surfaced entry.
    all_ids = (
        {r["handoff_id"] for r in second["reconciled"]}
        | {s["handoff_id"] for s in second["surfaced"]}
    )
    assert "2026-01-15-idempotent" not in all_ids
    assert _archived(handoff_repo, "2026-01-15-idempotent.md")


def test_ac9_empty_open_set_returns_cleanly(handoff_repo, tmp_path):
    """An empty open set (no handoffs, or none open) returns cleanly — no error,
    empty result lists."""
    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert result["reconciled"] == []
    assert result["gates_cleared"] == []
    assert result["surfaced"] == []


# ---------------------------------------------------------------------------
# F4 — error isolation: a mid-pass raise from ship_and_archive must not abort
# the whole enumeration; later nodes still process, the failed node is
# retried cleanly on the next invocation.
# ---------------------------------------------------------------------------


def test_f4_error_isolation_mid_pass_raise_does_not_abort_remaining_nodes(
    handoff_repo, tmp_path, monkeypatch
):
    """Force node K (of N=3) to fail inside ship_and_archive; assert K+1..N
    still process this pass, and K is cleanly retried (and succeeds) on a
    fresh re-invoke once the failure injection is removed."""
    node_ids = []
    for i in range(1, 4):
        deliverable = handoff_repo.root / f"f4_isolation_module_{i}.py"
        deliverable.write_text(f"# f4 isolation module {i}\n", encoding="utf-8")
        handoff_repo._git("add", f"f4_isolation_module_{i}.py")
        handoff_repo._git("commit", "-m", f"ship f4_isolation_module_{i} feature")

        name = f"2026-01-16-f4-node-{i}.md"
        handoff_repo.seed_handoff(
            name, "claimed",
            deployment_state="in_flight",
            claimed_at="2026-01-16T00:00:00Z", claimed_by=f"s-f4-{i}",
            extra=(
                f'title: "f4_isolation_module_{i} rollout"\n'
                f"scope:\n  - f4_isolation_module_{i}.py"
            ),
        )
        node_ids.append(f"2026-01-16-f4-node-{i}")

    failing_id = node_ids[1]  # node K = the 2nd of 3

    import coordinator_core.ops.handoff_reconcile as hr_mod
    real_ship_and_archive = hr_mod._ship_and_archive_handler

    async def _flaky_ship_and_archive(params, repo_root):
        if failing_id in params.get("handoff_path", ""):
            raise RuntimeError("simulated concurrent-session git conflict")
        return await real_ship_and_archive(params, repo_root)

    monkeypatch.setattr(hr_mod, "_ship_and_archive_handler", _flaky_ship_and_archive)

    policy_path = _write_enabled_policy(tmp_path)
    result = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    entries_by_id = {r["handoff_id"]: r for r in result["reconciled"]}
    assert set(entries_by_id) == set(node_ids), result["reconciled"]

    # K failed — recorded as an error entry, NOT applied, NOT archived.
    failing_entry = entries_by_id[failing_id]
    assert failing_entry["applied"] is False, failing_entry
    assert "simulated concurrent-session git conflict" in failing_entry["error"]
    assert not _archived(handoff_repo, f"{failing_id}.md")

    # K+1..N (here, the 1st and 3rd nodes) still processed and shipped —
    # the mid-pass raise did NOT abort the remaining enumeration.
    for other_id in node_ids:
        if other_id == failing_id:
            continue
        assert entries_by_id[other_id]["applied"] is True, entries_by_id[other_id]
        assert _archived(handoff_repo, f"{other_id}.md")

    # Re-invoke WITHOUT the failure injection — K is retried and now succeeds
    # cleanly (still open, since the first pass never mutated it).
    monkeypatch.undo()
    second = _run(_handler({"policy_path": policy_path}, handoff_repo.common_dir))
    assert second["exit_code"] == 0, second
    retry_entries = {r["handoff_id"]: r for r in second["reconciled"]}
    assert failing_id in retry_entries, second["reconciled"]
    assert retry_entries[failing_id]["applied"] is True, retry_entries[failing_id]
    assert _archived(handoff_repo, f"{failing_id}.md")


# ---------------------------------------------------------------------------
# scan_incomplete / scan_errors — unscannable archive/handoffs subtree
# (silent-success audit, state/audits/2026-07-22 shape): a permission-denied
# archive subtree feeds BOTH the C3 gate index (_collect_all_handoffs_for_gate_index)
# and the C6 dag_index (_collect_all_handoff_paths) — neither must silently read
# as "archive genuinely empty".
# ---------------------------------------------------------------------------

_SKIP_CHMOD = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)


@_SKIP_CHMOD
def test_collect_all_handoffs_for_gate_index_flags_unreadable_archive(tmp_path):
    """Unit-level round-trip: an unreadable archive/handoffs/ subtree makes
    _collect_all_handoffs_for_gate_index return non-empty scan_errors — never
    silently empty, and distinguishable from a genuinely-empty archive."""
    from coordinator_core.ops.handoff_reconcile import _collect_all_handoffs_for_gate_index

    worktree = tmp_path / "repo"
    (worktree / "state" / "handoffs").mkdir(parents=True)
    archive_dir = worktree / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-01-01-blocked.md").write_text(
        "---\ntitle: \"Blocked\"\nstatus: active\n---\n", encoding="utf-8"
    )

    # Baseline: genuinely-empty archive (readable, but no files) has clean scan_errors.
    empty_worktree = tmp_path / "repo-empty"
    (empty_worktree / "state" / "handoffs").mkdir(parents=True)
    (empty_worktree / "archive" / "handoffs").mkdir(parents=True)
    _empty_handoffs, empty_scan_errors = _collect_all_handoffs_for_gate_index(empty_worktree)
    assert empty_scan_errors == []

    original_mode = archive_dir.stat().st_mode
    os.chmod(archive_dir, 0o000)
    try:
        all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(worktree)
    finally:
        os.chmod(archive_dir, original_mode)

    assert all_handoffs == [], (
        "the unreadable archived handoff cannot be read — empty result IS expected here; "
        "the assertion below is what distinguishes this from the genuinely-empty case"
    )
    assert scan_errors, "scan_errors must be non-empty on an unscannable archive subtree"
    assert any(str(archive_dir) in e for e in scan_errors), scan_errors


@_SKIP_CHMOD
def test_collect_all_handoffs_for_gate_index_flags_unreadable_archive_completed(tmp_path):
    """Same round-trip as the sibling `archive/handoffs/` test above, but for the
    `archive/completed/` root C18a adds coverage for: an unreadable subtree there
    must ALSO make `_collect_all_handoffs_for_gate_index` return non-empty
    scan_errors, not silently read as an archive with nothing under it."""
    from coordinator_core.ops.handoff_reconcile import _collect_all_handoffs_for_gate_index

    worktree = tmp_path / "repo"
    (worktree / "state" / "handoffs").mkdir(parents=True)
    (worktree / "archive" / "handoffs").mkdir(parents=True)
    archive_completed_dir = worktree / "archive" / "completed"
    archive_completed_dir.mkdir(parents=True)
    (archive_completed_dir / "2026-01-01-done.md").write_text(
        "---\ntitle: \"Done\"\nstatus: consumed\n---\n", encoding="utf-8"
    )

    original_mode = archive_completed_dir.stat().st_mode
    os.chmod(archive_completed_dir, 0o000)
    try:
        all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(worktree)
    finally:
        os.chmod(archive_completed_dir, original_mode)

    assert all_handoffs == [], (
        "the unreadable archive/completed/ entry cannot be read — empty result IS "
        "expected here; the assertion below is what distinguishes this from the "
        "genuinely-empty case"
    )
    assert scan_errors, "scan_errors must be non-empty on an unscannable archive/completed/ subtree"
    assert any(str(archive_completed_dir) in e for e in scan_errors), scan_errors


def test_collect_all_handoffs_for_gate_index_attaches_path_to_archived_entry(tmp_path):
    """`ownership_index.build_ownership_index` resolves claim-store basenames
    against this function's own output rather than re-walking the corpus —
    that join only works if every returned entry, archived ones included,
    carries a usable `_path`. Pins the field so a future edit can't silently
    drop it back to the bare-frontmatter shape this function used to return."""
    from coordinator_core.ops.handoff_reconcile import _collect_all_handoffs_for_gate_index

    worktree = tmp_path / "repo"
    (worktree / "state" / "handoffs").mkdir(parents=True)
    archive_dir = worktree / "archive" / "handoffs" / "2026-07"
    archive_dir.mkdir(parents=True)
    archived_path = archive_dir / "archived-mid-ceremony.md"
    archived_path.write_text(
        "---\ntitle: \"Archived\"\nstatus: consumed\n---\n", encoding="utf-8"
    )

    all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(worktree)

    assert scan_errors == []
    assert len(all_handoffs) == 1
    assert all_handoffs[0]["_path"] == str(archived_path)


@_SKIP_CHMOD
def test_collect_all_handoff_paths_flags_unreadable_archive(tmp_path):
    """Unit-level round-trip for the C6 dag_index builder: same unreadable-archive
    shape, scan_errors must fire so the per-ancestor liveness gate's caller knows
    the dag_index may be undercounting live children (fail-open risk)."""
    from coordinator_core.ops.handoff_reconcile import _collect_all_handoff_paths

    worktree = tmp_path / "repo"
    (worktree / "state" / "handoffs").mkdir(parents=True)
    archive_dir = worktree / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-01-01-blocked.md").write_text(
        "---\ntitle: \"Blocked\"\nstatus: active\n---\n", encoding="utf-8"
    )

    original_mode = archive_dir.stat().st_mode
    os.chmod(archive_dir, 0o000)
    try:
        paths, scan_errors = _collect_all_handoff_paths(worktree)
    finally:
        os.chmod(archive_dir, original_mode)

    assert paths == [], (
        "the unreadable archived handoff cannot be read — empty result IS expected here; "
        "the assertion below is what distinguishes this from the genuinely-empty case"
    )
    assert scan_errors, "scan_errors must be non-empty on an unscannable archive subtree"
    assert any(str(archive_dir) in e for e in scan_errors), scan_errors


@_SKIP_CHMOD
def test_handler_surfaces_scan_incomplete_on_unreadable_archive(handoff_repo):
    """Full-op integration: an unreadable archive/handoffs/ subtree flags the
    top-level result scan_incomplete=True/scan_errors non-empty, distinguishable
    from a clean run over the same otherwise-benign open set."""
    handoff_repo.seed_handoff(
        "2026-01-20-unrelated-open.md", "open", deployment_state="open",
        extra='title: "unrelated open work"\nscope:\n  - nonexistent-path.py',
    )

    archive_dir = handoff_repo.root / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-01-20-archived.md").write_text(
        "---\ntitle: \"Archived\"\nstatus: claimed\n---\n", encoding="utf-8"
    )

    # Baseline: same fixture, archive readable — scan_incomplete is False.
    clean_result = _run(_handler({"dry_run": True}, handoff_repo.common_dir))
    assert clean_result.get("scan_incomplete") is False, clean_result
    assert clean_result.get("scan_errors") == [], clean_result

    # D1 (severed-observer gate): the baseline call above already persisted
    # "2026-01-20-unrelated-open" into the run-history as surfaced[]. Reset it
    # so the chmod'd call below is evaluated as its OWN first pass — this test
    # is exercising scan_incomplete plumbing, not D1's conservation assertion
    # (that gate has its own dedicated coverage in test_handoff_reconcile_d1.py).
    _run(_save_surfaced_history(
        _history_path(handoff_repo.common_dir), {}, handoff_repo.common_dir,
    ))

    original_mode = archive_dir.stat().st_mode
    os.chmod(archive_dir, 0o000)
    try:
        result = _run(_handler({"dry_run": True}, handoff_repo.common_dir))
    finally:
        os.chmod(archive_dir, original_mode)

    assert result["exit_code"] == 0, result
    assert result.get("scan_incomplete") is True, (
        f"scan_incomplete must be True when archive/handoffs/ cannot be scanned — "
        f"got {result.get('scan_incomplete')!r} (result keys: {sorted(result.keys())})"
    )
    assert result.get("scan_errors"), "scan_errors must be non-empty on an unscannable subtree"
    assert any(str(archive_dir) in e for e in result["scan_errors"]), result["scan_errors"]


@_SKIP_CHMOD
def test_handler_surfaced_evidence_names_scan_gap_not_dangling_ref_on_unreadable_archive(
    handoff_repo,
):
    """Review: code-reviewer (Finding 1) — integration regression for the
    _handler -> evaluate_gate wiring gap: an awaiting_gate handoff with an
    unresolvable blocked_by id, under a genuinely-unscannable archive subtree,
    must surface with evidence naming the scan gap ("cannot confirm ...
    archive scan incomplete") — NOT the "dangling blocked_by ref(s) ...
    unresolvable in live+archived index" confirmed-defect framing that fires
    when the archive is fully readable and the id is genuinely absent. Before
    the Finding 1 fix, `_handler` never threaded gate_index_scan_errors into
    its `evaluate_gate` call, so this always read as a confirmed data defect
    regardless of scan_incomplete — even though the top-level
    result["scan_incomplete"] was simultaneously True in the same response.
    """
    handoff_repo.seed_handoff(
        "2026-01-20-gate-scan-gap.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra("gate-scan-gap", "['stub-missing-under-scan-gap']"),
    )

    archive_dir = handoff_repo.root / "archive" / "handoffs"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-01-20-archived.md").write_text(
        "---\ntitle: \"Archived\"\nstatus: claimed\n---\n", encoding="utf-8"
    )

    original_mode = archive_dir.stat().st_mode
    os.chmod(archive_dir, 0o000)
    try:
        result = _run(_handler({"dry_run": True}, handoff_repo.common_dir))
    finally:
        os.chmod(archive_dir, original_mode)

    assert result["exit_code"] == 0, result
    assert result.get("scan_incomplete") is True, result

    entry = next(
        (s for s in result["surfaced"] if s.get("handoff_id") == "2026-01-20-gate-scan-gap"),
        None,
    )
    assert entry is not None, (
        f"expected the unresolved-blocked_by handoff in surfaced[], got {result['surfaced']!r}"
    )
    evidence_text = " ".join(str(e) for e in entry.get("evidence") or [])
    assert "cannot confirm" in evidence_text and "scan incomplete" in evidence_text, (
        f"surfaced[] evidence must name the scan gap ('cannot confirm ... archive scan "
        f"incomplete'), not assert a confirmed dangling ref, when scan_incomplete=True — "
        f"got: {evidence_text!r}"
    )
    assert "dangling" not in evidence_text, (
        f"evidence must NOT read as a confirmed dangling-ref data defect while the archive "
        f"scan is incomplete — got: {evidence_text!r}"
    )
