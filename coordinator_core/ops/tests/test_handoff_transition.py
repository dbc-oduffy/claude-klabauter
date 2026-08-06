"""
coordinator_core.ops.tests.test_handoff_transition

Tests for the handoff.transition op (claim / supersede / ship verbs).
"consume"/"unconsume" are DR-084 deprecated aliases of "claim"/"unclaim" —
see test_consume_unconsume_deprecated_aliases_still_work for their dispatch
coverage; the helper builders below (_consume_params/_unconsume_params)
emit the canonical claim/unclaim verb for every other test in this file.

Import guard: coordinator_core.ops.handoff_transition MUST be imported at module
load time to fire @register_op("handoff.transition") and populate _REGISTRY.
Without this the decorator has not run and any registry-completeness assertion
passes vacuously over an empty entry.

Coverage:
  (a) consume — applies status→consumed, deployment_state→in_flight, claimed_at,
                claimed_by; verifies writes to the REAL state/handoffs file (P9)
  (b) consume idempotency — full target state (consumed+in_flight) is a no-op
  (c) consume partial state — consumed+non-in_flight completes the transition
  (d) consume empty session_id — exit_code=1, no write (the Staff Engineer P2 fail-loud)
  (e) supersede — applies status→claimed, deployment_state→continued+continued_into
  (f) supersede idempotency — full target state (claimed+continued+continued_into) is a no-op
  (g) ship — sets deployment_state→shipped; status untouched
  (h) ship idempotency — already-shipped is a no-op
  (i) over-cap summary rejection — summary >140 chars + created≥2026-05-29 blocks write
  (j) LockTimeout → exit_code=1 error result for all three verbs (fail-closed)
  (k) repark — flips deployment_state in_flight→ready_to_fire; status untouched;
                idempotent no-op at ready_to_fire; fail-loud on non-in_flight source
  (l) gate-recheck — --cleared flip (awaiting_gate→ready_to_fire, gate_dependency
                stripped, last_gate_recheck stamped); bare stamp-only path;
                idempotent no-op at ready_to_fire with --cleared; fail-loud on
                non-awaiting_gate source; schema-reject on malformed frontmatter
  (l2a) gate_evidence live re-resolution against a REAL git sibling (AC7) —
                frontmatter-field and commit-ancestor decoded from their
                composite refs ('<path>#<field>', '<commit-ish>@<target-ref>')
                in BOTH polarities, with the persisted per-leg statuses proved
                to come from the sibling's disk and not the record's prose;
                plus the fail-loud rejection of an unrecognised kind that
                formerly fell through into a commit_ancestor request
  (m) gate-cascade-clear — structured blocked_by narrow-or-flip: all-shipped
                flips to ready_to_fire + gate_cleared_by stamped + gate_dependency
                stripped ENTIRELY; partial-shipped narrows (blocked_by shrinks,
                stays awaiting_gate, gate_dependency partially reduced not fully
                stripped); act-time re-verification fails loud (no write) on a
                stale/regressed shipped claim or an unresolvable blocker id —
                never trusts the caller-supplied verdict; fails loud on
                blocker_ids/blocker_shas asymmetry, non-awaiting_gate source,
                a requested id absent from blocked_by; idempotent no-op at full
                target state (blocked_by empty + ready_to_fire)

Spec backlinks:
  - Port source: example-doctrine-repo coordinator/bin/handoff-transition.js
  - Plan C3: docs/plans/2026-07-05-pcore-12-handoff-transition-op.md (forthcoming)
  - Plan C1 (gate-recheck + repark): docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C1
  - Plan C8 (gate-cascade-clear): docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C8
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("handoff.transition") as a side-effect.
# MUST precede any test function so the registry is populated before assertions.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op

import coordinator_core.ops.handoff_transition as _mod

from coordinator_core.ipc import _REGISTRY
from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.handoff_transition import _handler
from coordinator_core.frontmatter.primitives import split_frontmatter, read_fm_field

# ---------------------------------------------------------------------------
# Registry completeness assertion
# ---------------------------------------------------------------------------

_OP_NAME = "handoff.transition"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_transition @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


_AT = "2026-01-02T10:00:00Z"
_AT_DATE = "2026-01-02"
_SESSION_ID = "session-test-abc123"


def _consume_params(handoff_path: str, session_id: str = _SESSION_ID, at: str = _AT) -> dict:
    # Builds the CANONICAL "claim" verb (DR-084 rename) — see
    # test_consume_unconsume_deprecated_aliases_still_work below for the
    # dedicated old-spelling ("consume"/"unconsume") dispatch coverage.
    return {"verb": "claim", "handoff_path": handoff_path, "session_id": session_id, "at": at}


def _supersede_params(handoff_path: str, continued_into: str = "2026-01-02-successor.md") -> dict:
    return {"verb": "supersede", "handoff_path": handoff_path, "continued_into": continued_into}


def _ship_params(handoff_path: str) -> dict:
    return {"verb": "ship", "handoff_path": handoff_path}


def _repark_params(handoff_path: str) -> dict:
    return {"verb": "repark", "handoff_path": handoff_path}


def _unconsume_params(handoff_path: str, note: str = "") -> dict:
    # Builds the CANONICAL "unclaim" verb (DR-084 rename) — see
    # test_consume_unconsume_deprecated_aliases_still_work below for the
    # dedicated old-spelling ("consume"/"unconsume") dispatch coverage.
    params = {"verb": "unclaim", "handoff_path": handoff_path}
    if note:
        params["note"] = note
    return params


def _gate_recheck_params(handoff_path: str, at: str = _AT_DATE, cleared: bool = False) -> dict:
    return {"verb": "gate-recheck", "handoff_path": handoff_path, "at": at, "cleared": cleared}


def _read_fm(path_str: str) -> str:
    """Return the fm_text of the file at path_str, or raise if no frontmatter."""
    content = open(path_str, encoding="utf-8").read()
    split = split_frontmatter(content)
    assert split is not None, f"no parseable frontmatter in {path_str}"
    return split.fm_text


# ---------------------------------------------------------------------------
# (a) consume — full happy-path transition + P9 real-path write verification
# ---------------------------------------------------------------------------


def test_consume_applies_full_transition(handoff_repo):
    """consume transitions status→consumed, deployment_state→in_flight, adds timestamps.

    Semantic correctness is proven by the field assertions below.  The P9 contract
    (main_worktree_root derivation) is exercised exclusively in
    test_consume_relative_path_resolves_from_worktree — see Review note below.

    Review: code-reviewer (F4) — removed false-positive P9 label and os.path.isfile
    assertion from this test. The absolute-path invocation here never calls
    main_worktree_root, so the P9 assertion was trivially true whether or not
    main_worktree_root was broken. The P9 contract belongs solely in the relative-path test.
    """
    handoff_repo.seed_handoff("2026-01-01-consume-happy.md", "open")
    abs_path = handoff_repo.abs_path("2026-01-01-consume-happy.md")

    result = _run(_handler(
        _consume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "claimed"
    assert read_fm_field(fm, "deployment_state") == "in_flight"
    # claimed_at is YAML-quoted by serialize_yaml_scalar (ISO timestamps contain ':')
    # so read_fm_field returns "'2026-01-02T10:00:00Z'" — use 'in' to avoid quote sensitivity.
    assert _AT in (read_fm_field(fm, "claimed_at") or ""), (
        f"claimed_at must contain {_AT!r}; got {read_fm_field(fm, 'claimed_at')!r}"
    )
    assert read_fm_field(fm, "claimed_by") == _SESSION_ID


def test_consume_relative_path_resolves_from_worktree(handoff_repo):
    """consume accepts a repo-relative handoff_path and resolves it against the worktree."""
    handoff_repo.seed_handoff("2026-01-01-relative.md", "open")

    result = _run(_handler(
        _consume_params("state/handoffs/2026-01-01-relative.md"),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(handoff_repo.abs_path("2026-01-01-relative.md"))
    assert read_fm_field(fm, "status") == "claimed"
    assert read_fm_field(fm, "deployment_state") == "in_flight"


def test_consume_strips_gate_evidence(handoff_repo):
    """C7 (AC10): consume strips gate_evidence on the awaiting_gate->in_flight
    pickup -- unlike gate_dependency (open bug, backlog-deferred: consume is
    that bug's own root cause), gate_evidence must not inherit the same hole
    on day one (nested-block REMOVE, not remove_fm_field)."""
    handoff_repo.seed_handoff(
        "2026-01-01-consume-strips-evidence.md", "open",
        deployment_state="awaiting_gate",
        extra=(
            "blocking_notes: waiting on manual check\n"
            "gate_evidence:\n"
            "  covers_prose: true\n"
            "  legs:\n"
            "    - kind: human\n"
            "      reason: manual check pending"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-consume-strips-evidence.md")

    result = _run(_handler(
        _consume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "in_flight"
    assert "gate_evidence:" not in fm, "gate_evidence key must not appear on disk at all"
    assert "manual check pending" not in fm, "no orphaned continuation lines"


# ---------------------------------------------------------------------------
# (b) consume idempotency — full target state is a no-op
# ---------------------------------------------------------------------------


def test_consume_idempotent_at_full_target_state(handoff_repo):
    """consume is a no-op when the full target state (consumed+in_flight) already holds."""
    handoff_repo.seed_handoff(
        "2026-01-01-already-consumed.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-already-consumed.md")
    original_content = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _consume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False, "already-at-target must be a no-op (applied=False)"

    # File must be byte-identical to original.
    assert open(abs_path, encoding="utf-8").read() == original_content, (
        "idempotent no-op must not modify the file"
    )


def test_consume_restamps_claimed_by_on_stale_claim_takeover(handoff_repo):
    """2026-07-24 regression: a stale-claim takeover (full target state already
    holds — claimed+in_flight — but the recorded claimed_by names a DIFFERENT,
    now-dead session) must re-stamp claimed_by (and claimed_at) to the NEW
    session rather than silently no-op'ing on status/deployment_state alone.

    This is the frontmatter half of the takeover: coordinator_core.session.
    claims.claim_artifact already hands the authoritative claim LOCK to the
    new session on takeover; this verb keeps the frontmatter in agreement so
    /workstream-complete's Detector A (state/handoffs/ claimed_by==my-sid
    scan) can find the handoff instead of falling through to an unrelated
    coincidental-overlap match.
    """
    _OLD_SESSION_ID = "session-test-dead-holder"
    _NEW_SESSION_ID = "session-test-new-holder"
    handoff_repo.seed_handoff(
        "2026-01-01-takeover.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_OLD_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-takeover.md")

    result = _run(_handler(
        _consume_params(abs_path, session_id=_NEW_SESSION_ID, at="2026-02-03T11:00:00Z"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True, "a holder change must re-stamp, not no-op"

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "claimed"
    assert read_fm_field(fm, "deployment_state") == "in_flight"
    assert read_fm_field(fm, "claimed_by") == _NEW_SESSION_ID, (
        "claimed_by must be re-stamped to the new (takeover) session id, "
        "never left naming the dead prior holder"
    )
    assert "2026-02-03T11:00:00Z" in (read_fm_field(fm, "claimed_at") or ""), (
        "claimed_at must be re-stamped to the takeover instant"
    )


def test_consume_idempotent_when_same_holder_reinvoked(handoff_repo):
    """Sanity companion to the takeover test: re-invoking consume with the SAME
    session id that already holds the claim stays a byte-identical no-op —
    the holder-mismatch check must not turn every re-invocation into a write.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-same-holder.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-same-holder.md")
    original_content = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _consume_params(abs_path, session_id=_SESSION_ID),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original_content


# ---------------------------------------------------------------------------
# (c) consume partial state — completes the transition (D5)
# ---------------------------------------------------------------------------


def test_consume_completes_partial_state_consumed_not_in_flight(handoff_repo):
    """Partial state (status=consumed, deployment_state=ready_to_fire) is completed.

    D5: idempotency guards fire ONLY at the FULL target state.  A prior normalize
    sweep may have flipped status without setting deployment_state → in_flight; the
    consume verb must complete the transition rather than short-circuit.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-partial.md", "claimed",
        deployment_state="ready_to_fire",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-partial.md")

    result = _run(_handler(
        _consume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True, "partial state must be completed (applied=True)"

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "claimed"
    assert read_fm_field(fm, "deployment_state") == "in_flight", (
        "deployment_state must be updated from ready_to_fire → in_flight"
    )
    assert read_fm_field(fm, "claimed_at") is not None
    assert read_fm_field(fm, "claimed_by") == _SESSION_ID


# ---------------------------------------------------------------------------
# (d) consume empty session_id — fail-loud, no write (the Staff Engineer P2)
# ---------------------------------------------------------------------------


def test_consume_empty_session_id_returns_error(handoff_repo):
    """consume with empty session_id returns exit_code=1 and does NOT write the file."""
    handoff_repo.seed_handoff("2026-01-01-empty-sid.md", "open")
    abs_path = handoff_repo.abs_path("2026-01-01-empty-sid.md")
    original_content = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _consume_params(abs_path, session_id=""),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"empty session_id must return exit_code=1; got {result!r}"
    )
    assert result["applied"] is False

    # File must be unmodified.
    assert open(abs_path, encoding="utf-8").read() == original_content, (
        "file must not be modified when session_id is empty"
    )


def test_consume_whitespace_only_session_id_returns_error(handoff_repo):
    """consume with whitespace-only session_id also fails loud (strip-then-empty check)."""
    handoff_repo.seed_handoff("2026-01-01-ws-sid.md", "open")
    abs_path = handoff_repo.abs_path("2026-01-01-ws-sid.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _consume_params(abs_path, session_id="   "),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert open(abs_path, encoding="utf-8").read() == original


def test_consume_unconsume_deprecated_aliases_still_work(handoff_repo):
    """DR-084 verb rename (consume->claim, unconsume->unclaim): the OLD verb
    spellings must keep dispatching to the SAME transitions as the new ones —
    this is the load-bearing property of the rename, pinned as its own
    assertion rather than left to incidental coverage."""
    handoff_repo.seed_handoff("2026-01-01-alias.md", "open")
    abs_path = handoff_repo.abs_path("2026-01-01-alias.md")

    claimed = _run(_handler(
        {"verb": "consume", "handoff_path": abs_path, "session_id": _SESSION_ID, "at": _AT},
        repo_root=handoff_repo.common_dir,
    ))
    assert claimed["exit_code"] == 0
    assert claimed["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "claimed"
    assert read_fm_field(fm, "deployment_state") == "in_flight"
    assert read_fm_field(fm, "claimed_by") == _SESSION_ID

    unclaimed = _run(_handler(
        {"verb": "unconsume", "handoff_path": abs_path},
        repo_root=handoff_repo.common_dir,
    ))
    assert unclaimed["exit_code"] == 0
    assert unclaimed["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "claimed_by") is None


# ---------------------------------------------------------------------------
# (e) supersede — full happy-path transition
# ---------------------------------------------------------------------------


def test_supersede_applies_transition(handoff_repo):
    """supersede transitions status→claimed, deployment_state→continued+continued_into; no timestamps."""
    # DR-242 (Finding 1, C5 review fix): verb="supersede" now gates on
    # claimed_or_shipped at the op choke point — seed a legitimately claimed-
    # or-shipped predecessor via `shipped_in` (rather than claimed_at/
    # claimed_by, which this test asserts supersede itself never writes) so
    # the gate passes without contaminating the "no timestamps written"
    # assertions below.
    handoff_repo.seed_handoff(
        "2026-01-01-supersede.md", "open", shipped_in="deadbeef",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-supersede.md")

    result = _run(_handler(
        _supersede_params(abs_path, continued_into="2026-01-02-successor.md"),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "claimed"
    assert read_fm_field(fm, "deployment_state") == "continued"
    assert "2026-01-02-successor.md" in (read_fm_field(fm, "continued_into") or "")
    # No claimed_at or claimed_by written for supersession.
    assert read_fm_field(fm, "claimed_at") is None, (
        "supersede must NOT write claimed_at"
    )
    assert read_fm_field(fm, "claimed_by") is None, (
        "supersede must NOT write claimed_by"
    )


# ---------------------------------------------------------------------------
# (f) supersede idempotency — claimed+continued+continued_into is a no-op
# ---------------------------------------------------------------------------


def test_supersede_idempotent_at_full_target_state(handoff_repo):
    """supersede is a no-op when status==claimed AND deployment_state==continued
    AND continued_into already equals the supplied successor."""
    handoff_repo.seed_handoff(
        "2026-01-01-already-superseded.md", "claimed",
        deployment_state="continued",
        extra="continued_into: 2026-01-02-successor.md",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-already-superseded.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _supersede_params(abs_path, continued_into="2026-01-02-successor.md"),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (f2) supersede refuses to overwrite a human-closed terminal (symmetric
# counterpart of _close's shipped|continued refusal)
# ---------------------------------------------------------------------------


def test_supersede_refuses_closed_target(handoff_repo):
    """supersede refuses (exit_code=1, no write) when deployment_state is
    already 'closed' — reversing a deliberate human close is a human
    decision, not an automated writer's to make. The error names the
    closed_reason found on disk."""
    handoff_repo.seed_handoff(
        "2026-01-01-closed.md", "claimed",
        deployment_state="closed",
        extra="closed_reason: stale",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-closed.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _supersede_params(abs_path, continued_into="2026-01-02-successor.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "stale" in result.get("error", ""), (
        f"error must name the closed_reason found; got {result.get('error')!r}"
    )
    assert open(abs_path, encoding="utf-8").read() == original, (
        "supersede refusal must leave the on-disk file unchanged"
    )


def test_supersede_still_no_ops_when_already_continued(handoff_repo):
    """The new closed-target refusal must not disturb the pre-existing
    already-continued idempotency no-op — ordering matters (idempotency
    checked BEFORE the new refusal)."""
    handoff_repo.seed_handoff(
        "2026-01-01-already-continued.md", "claimed",
        deployment_state="continued",
        extra="continued_into: 2026-01-02-successor.md",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-already-continued.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _supersede_params(abs_path, continued_into="2026-01-02-successor.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_supersede_still_applies_to_ordinary_nonterminal_target(handoff_repo):
    """An ordinary non-terminal target (deployment_state:open, no
    closed_reason) is unaffected by the new guard and still supersedes
    normally."""
    handoff_repo.seed_handoff(
        "2026-01-01-ordinary.md", "open", shipped_in="deadbeef",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-ordinary.md")

    result = _run(_handler(
        _supersede_params(abs_path, continued_into="2026-01-02-successor.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "continued"


# ---------------------------------------------------------------------------
# (g) ship — sets deployment_state→shipped; status is untouched
# ---------------------------------------------------------------------------


def test_ship_applies_transition_status_untouched(handoff_repo):
    """ship sets deployment_state→shipped; the status field is NOT modified.

    Uses an old created date (2026-01-01) so the shipped_in cross-field rule
    (required for created >= 2026-05-29) does not fire.
    """
    handoff_repo.seed_handoff("2026-01-01-ship.md", "open")
    abs_path = handoff_repo.abs_path("2026-01-01-ship.md")
    original_status = read_fm_field(_read_fm(abs_path), "status")

    result = _run(_handler(
        _ship_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "shipped"
    assert read_fm_field(fm, "status") == original_status, (
        "ship must NOT modify the status field"
    )


def test_ship_applies_when_existing_deployment_state_replaced(handoff_repo):
    """ship replaces an existing deployment_state (not just inserts it)."""
    handoff_repo.seed_handoff("2026-01-01-ship-replace.md", "open",
                               deployment_state="ready_to_fire")
    abs_path = handoff_repo.abs_path("2026-01-01-ship-replace.md")

    result = _run(_handler(
        _ship_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "shipped"


# ---------------------------------------------------------------------------
# (h) ship idempotency — already-shipped is a no-op
# ---------------------------------------------------------------------------


def test_ship_idempotent_when_already_shipped(handoff_repo):
    """ship is a no-op when deployment_state is already 'shipped'."""
    # Use a pre-cutoff date and include shipped_in to satisfy any cross-field rules
    # that may fire on the seed (keeping the seeded file schema-valid is not strictly
    # required here since we're testing the idempotency path, but it's good hygiene).
    handoff_repo.seed_handoff(
        "2026-01-01-already-shipped.md", "claimed",
        deployment_state="shipped",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-already-shipped.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _ship_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (i) over-cap summary rejection — schema validation blocks the write
# ---------------------------------------------------------------------------


def test_consume_rejects_over_cap_summary(handoff_repo):
    """consume returns exit_code=1 and does NOT write when summary exceeds 140 chars.

    The summary cross-field rule fires when created >= '2026-05-29' AND summary
    is present AND len(summary) > 140.  The validation gate runs AFTER in-memory
    mutation but BEFORE the file write, so the on-disk file remains unchanged.
    """
    long_summary = "A" * 141  # 141 chars → exceeds the 140-char cap
    handoff_repo.seed_handoff(
        "2026-06-01-overcap.md", "open",
        created="2026-06-01",
        branch="work/test/2026-06-01",
        category="infra",
        summary=long_summary,
    )
    abs_path = handoff_repo.abs_path("2026-06-01-overcap.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _consume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"over-cap summary must block the write (exit_code=1); got {result!r}"
    )
    assert result["applied"] is False
    assert "summary" in result.get("error", "").lower() or "140" in result.get("error", ""), (
        f"error message must mention 'summary' or '140'; got {result.get('error')!r}"
    )

    # File must be unmodified — the gate prevented the write.
    assert open(abs_path, encoding="utf-8").read() == original, (
        "over-cap rejection must leave the on-disk file unchanged"
    )


def test_supersede_rejects_over_cap_summary(handoff_repo):
    """supersede also rejects handoffs with over-cap summary (same validation gate)."""
    long_summary = "B" * 141
    handoff_repo.seed_handoff(
        "2026-06-01-supersede-overcap.md", "open",
        created="2026-06-01",
        branch="work/test/2026-06-01",
        category="infra",
        summary=long_summary,
    )
    abs_path = handoff_repo.abs_path("2026-06-01-supersede-overcap.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _supersede_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (j) LockTimeout → exit_code=1 error result (fail-closed)
#
# Verifies that locked_rmw raising LockTimeout is mapped to exit_code=1 by each
# verb, not propagated as an uncaught exception.  Uses monkeypatch to inject a
# simulated LockTimeout without needing a real competing process.
# ---------------------------------------------------------------------------


def _raise_lock_timeout(*args, **kwargs):
    """Replacement for locked_rmw that always raises LockTimeout (test helper)."""
    raise LockTimeout("simulated lock timeout for test")


def test_consume_lock_timeout_returns_error(handoff_repo, monkeypatch):
    """consume maps LockTimeout to exit_code=1 error result; no write is attempted."""
    handoff_repo.seed_handoff("2026-01-01-consume-timeout.md", "open")
    abs_path = handoff_repo.abs_path("2026-01-01-consume-timeout.md")
    original = open(abs_path, encoding="utf-8").read()

    monkeypatch.setattr(_mod, "locked_rmw", _raise_lock_timeout)

    result = _run(_handler(
        _consume_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"LockTimeout must map to exit_code=1 (fail-closed); got {result!r}"
    )
    assert result["applied"] is False
    # Error message must reference the timeout/lock situation.
    error_msg = result.get("error", "")
    assert "timeout" in error_msg.lower() or "lock" in error_msg.lower(), (
        f"error message must mention 'timeout' or 'lock'; got {error_msg!r}"
    )
    # File must be unmodified — locked_rmw never ran.
    assert open(abs_path, encoding="utf-8").read() == original, (
        "LockTimeout must leave the file unchanged"
    )


def test_supersede_lock_timeout_returns_error(handoff_repo, monkeypatch):
    """supersede maps LockTimeout to exit_code=1 error result; no write is attempted."""
    handoff_repo.seed_handoff("2026-01-01-supersede-timeout.md", "open")
    abs_path = handoff_repo.abs_path("2026-01-01-supersede-timeout.md")
    original = open(abs_path, encoding="utf-8").read()

    monkeypatch.setattr(_mod, "locked_rmw", _raise_lock_timeout)

    result = _run(_handler(
        _supersede_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"LockTimeout must map to exit_code=1 (fail-closed); got {result!r}"
    )
    assert result["applied"] is False
    error_msg = result.get("error", "")
    assert "timeout" in error_msg.lower() or "lock" in error_msg.lower(), (
        f"error message must mention 'timeout' or 'lock'; got {error_msg!r}"
    )
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (k) Path containment — traversal / out-of-tree / archive-path rejection
#
# handoff.transition is a MUTATING verb; its allowed root is state/handoffs/
# ONLY (archive/handoffs/ is deliberately excluded — mutating an archived
# handoff is out of scope for a live-lifecycle transition). See
# docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4.
# ---------------------------------------------------------------------------


def test_consume_rejects_traversal_path(handoff_repo):
    """A handoff_path with '../' traversal segments escaping state/handoffs/ is
    rejected with exit_code=1; the out-of-tree target file is NOT mutated."""
    # A secret file outside state/handoffs/ that a traversal could reach.
    secret = handoff_repo.root / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        _consume_params("state/handoffs/../../secret.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, f"traversal path must be rejected; got {result!r}"
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original, (
        "traversal must not mutate the out-of-tree target file"
    )


def test_consume_rejects_out_of_tree_absolute_path(tmp_path, handoff_repo):
    """An out-of-tree absolute handoff_path (outside state/handoffs/) is rejected."""
    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir(parents=True)
    outside.write_text('---\ntitle: "Secret"\n---\n', encoding="utf-8")
    original = outside.read_text(encoding="utf-8")

    result = _run(_handler(
        _consume_params(str(outside)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert outside.read_text(encoding="utf-8") == original


def test_consume_rejects_archive_handoffs_path(handoff_repo):
    """A handoff_path under archive/handoffs/ is rejected — mutation verbs are
    live-only (state/handoffs/ ONLY); archived handoffs are out of scope."""
    archived = handoff_repo.root / "archive" / "handoffs" / "2026-01" / "old.md"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        '---\ntitle: "Old"\ncreated: 2026-01-01\nbranch: work/test/2026-01-01\n'
        'status: active\npredecessor: "none"\n---\n\nBody.\n',
        encoding="utf-8",
    )
    original = archived.read_text(encoding="utf-8")

    result = _run(_handler(
        _consume_params(str(archived)),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"archive/handoffs/ path must be rejected for a mutation verb; got {result!r}"
    )
    assert result["applied"] is False
    assert archived.read_text(encoding="utf-8") == original


def test_supersede_rejects_traversal_path(handoff_repo):
    """supersede also rejects traversal paths — same containment guard."""
    secret = handoff_repo.root / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        _supersede_params("state/handoffs/../../secret.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original


def test_ship_rejects_traversal_path(handoff_repo):
    """ship also rejects traversal paths — same containment guard."""
    secret = handoff_repo.root / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        _ship_params("state/handoffs/../../secret.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original


def test_ship_lock_timeout_returns_error(handoff_repo, monkeypatch):
    """ship maps LockTimeout to exit_code=1 error result; no write is attempted."""
    handoff_repo.seed_handoff("2026-01-01-ship-timeout.md", "open")
    abs_path = handoff_repo.abs_path("2026-01-01-ship-timeout.md")
    original = open(abs_path, encoding="utf-8").read()

    monkeypatch.setattr(_mod, "locked_rmw", _raise_lock_timeout)

    result = _run(_handler(
        _ship_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"LockTimeout must map to exit_code=1 (fail-closed); got {result!r}"
    )
    assert result["applied"] is False
    error_msg = result.get("error", "")
    assert "timeout" in error_msg.lower() or "lock" in error_msg.lower(), (
        f"error message must mention 'timeout' or 'lock'; got {error_msg!r}"
    )
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (k) repark — flip, idempotent no-op, fail-loud on non-in_flight source
# ---------------------------------------------------------------------------


def test_repark_flips_in_flight_to_ready_to_fire(handoff_repo):
    """repark flips deployment_state in_flight→ready_to_fire; status untouched."""
    handoff_repo.seed_handoff(
        "2026-01-01-repark.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-repark.md")

    result = _run(_handler(
        _repark_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "status") == "claimed", "repark must NOT modify status"
    # The claim record is untouched (repark is a deployment_state-only unpause).
    assert read_fm_field(fm, "claimed_by") == _SESSION_ID
    assert _AT in (read_fm_field(fm, "claimed_at") or "")


def test_repark_strips_stale_gate_dependency_on_flip(handoff_repo):
    """repark strips a stale gate_dependency entirely on the in_flight→ready_to_fire flip.

    A node can reach in_flight while still carrying a stale gate_dependency (nothing
    strips it on the awaiting_gate→in_flight consume flip). Without the strip, the
    post-mutation schema gate's ready_to_fire→gate_dependency-forbidden cross-field
    rule (_cf_ready_to_fire_no_dependency) would fail-loud on this legitimate input.
    Cross-writer parity fix — mirrors gate-recheck --cleared and the example-doctrine-repo
    handoff-transition.js unconsume writer.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-repark-stale-gate-dep.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
        extra="gate_dependency: stale-condition",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-repark-stale-gate-dep.md")

    result = _run(_handler(
        _repark_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "gate_dependency") is None, (
        "gate_dependency must be STRIPPED entirely (key removed), not blanked"
    )
    assert "gate_dependency" not in fm, "gate_dependency key must not appear on disk at all"
    # C8/AC11: the prose is RETIRED, not destroyed -- it survives in blocking_notes.
    assert "stale-condition" in (read_fm_field(fm, "blocking_notes") or "")


def test_repark_strips_gate_evidence_on_flip(handoff_repo):
    """C7 (AC10): repark also strips a stale gate_evidence block (nested-block
    REMOVE, not remove_fm_field) on the same in_flight->ready_to_fire flip --
    _cf_ready_to_fire_no_gate_evidence would fail-loud on this input otherwise.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-repark-stale-gate-evidence.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
        extra=(
            "gate_evidence:\n"
            "  covers_prose: true\n"
            "  legs:\n"
            "    - kind: human\n"
            "      reason: manual check pending"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-repark-stale-gate-evidence.md")

    result = _run(_handler(
        _repark_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert "gate_evidence:" not in fm, "gate_evidence key must not appear on disk at all"
    assert "manual check pending" not in fm, "no orphaned continuation lines"


def test_repark_idempotent_when_already_ready_to_fire(handoff_repo):
    """repark is a no-op when deployment_state is already ready_to_fire."""
    handoff_repo.seed_handoff(
        "2026-01-01-repark-noop.md", "claimed",
        deployment_state="ready_to_fire",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-repark-noop.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _repark_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_repark_fails_loud_on_awaiting_gate_source(handoff_repo):
    """repark fails loud (non-zero exit, no write) when deployment_state is NOT in_flight.

    repark is defined ONLY as the in_flight → ready_to_fire transition — parking
    from awaiting_gate is not this verb's job (mirror example-doctrine-repo handoff-transition.js:385-389).
    """
    handoff_repo.seed_handoff(
        "2026-01-01-repark-fail.md", "open",
        deployment_state="awaiting_gate",
        extra="gate_dependency: some-condition",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-repark-fail.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _repark_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] != 0, (
        f"repark on an awaiting_gate handoff must fail loud; got {result!r}"
    )
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original, (
        "fail-loud repark must not modify the file"
    )


def test_repark_fails_loud_on_shipped_source(handoff_repo):
    """repark also fails loud on a shipped handoff (not in_flight)."""
    handoff_repo.seed_handoff(
        "2026-01-01-repark-shipped.md", "claimed",
        deployment_state="shipped",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-repark-shipped.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _repark_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] != 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (n) unconsume — clean pickup-reversal reset (inverse of consume)
# ---------------------------------------------------------------------------


def test_unconsume_applies_from_in_flight(handoff_repo):
    """unconsume resets consumed+in_flight → active+ready_to_fire, strips stamps,
    preserves pickup_ready untouched."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-in-flight.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
        extra="pickup_ready: true",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-in-flight.md")

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "claimed_at") is None, "claimed_at must be STRIPPED entirely"
    assert read_fm_field(fm, "claimed_by") is None, "claimed_by must be STRIPPED entirely"
    assert "claimed_at" not in fm
    assert "claimed_by" not in fm
    assert read_fm_field(fm, "pickup_ready") == "true", "pickup_ready must be preserved untouched"


def test_unconsume_strips_stale_gate_dependency_on_flip(handoff_repo):
    """unconsume strips a stale gate_dependency entirely on the flip to ready_to_fire.

    A node can reach in_flight while still carrying a stale gate_dependency (nothing
    strips it on the awaiting_gate→in_flight consume flip). Without the strip, the
    post-mutation schema gate's ready_to_fire→gate_dependency-forbidden cross-field
    rule (_cf_ready_to_fire_no_dependency) would fail-loud on this legitimate input.
    Cross-writer parity fix — mirrors gate-recheck --cleared and the example-doctrine-repo
    handoff-transition.js unconsume writer.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-stale-gate-dep.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
        extra="gate_dependency: stale-condition",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-stale-gate-dep.md")

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "gate_dependency") is None, (
        "gate_dependency must be STRIPPED entirely (key removed), not blanked"
    )
    assert "gate_dependency" not in fm, "gate_dependency key must not appear on disk at all"
    # C8/AC11: the prose is RETIRED, not destroyed -- it survives in blocking_notes.
    assert "stale-condition" in (read_fm_field(fm, "blocking_notes") or "")


def test_unconsume_strips_gate_evidence_on_flip(handoff_repo):
    """C7 (AC10): unconsume also strips a stale gate_evidence block (nested-block
    REMOVE) defensively on the same flip to ready_to_fire, mirroring its own
    defensive gate_dependency strip above."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-stale-gate-evidence.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
        extra=(
            "gate_evidence:\n"
            "  covers_prose: true\n"
            "  legs:\n"
            "    - kind: human\n"
            "      reason: manual check pending"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-stale-gate-evidence.md")

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert "gate_evidence:" not in fm, "gate_evidence key must not appear on disk at all"
    assert "manual check pending" not in fm, "no orphaned continuation lines"


def test_unconsume_applies_from_ready_to_fire(handoff_repo):
    """unconsume also applies from consumed+ready_to_fire — the cockpit
    reparked-but-stale incident case."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-ready.md", "claimed",
        deployment_state="ready_to_fire",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-ready.md")

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "claimed_at") is None
    assert read_fm_field(fm, "claimed_by") is None


def test_unconsume_idempotent_when_already_active(handoff_repo):
    """unconsume is a no-op ONLY at the full target state (active+ready_to_fire).

    Review: code-reviewer Finding 1 (P2) — the no-op guard keys on the FULL
    target state (status==active AND deployment_state==ready_to_fire),
    mirroring consume's D5 idempotency, not on status alone.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-noop.md", "open",
        deployment_state="ready_to_fire",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-noop.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_unconsume_normalizes_active_in_flight(handoff_repo):
    """An inconsistent active+in_flight record falls through and normalizes.

    Review: code-reviewer Finding 1 (P2) — status==active alone must NOT
    short-circuit as a no-op when deployment_state disagrees (e.g. in_flight,
    a stale/hand-edited shape). The full-target-state discriminator lets this
    fall through to complete the transition (deployment_state → ready_to_fire)
    rather than silently preserving the inconsistency.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-active-inflight.md", "open",
        deployment_state="in_flight",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-active-inflight.md")

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True, "active+in_flight must normalize, not no-op"

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"


def test_unconsume_fails_loud_on_shipped_source(handoff_repo):
    """unconsume fails loud (exit_code=1, no write) when deployment_state is shipped —
    out of scope, a different lifecycle question."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-shipped.md", "claimed",
        deployment_state="shipped",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-shipped.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"unconsume on a shipped handoff must fail loud; got {result!r}"
    )
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_unconsume_fails_loud_on_continued_source(handoff_repo):
    """unconsume also fails loud on a continued handoff (not in_flight/ready_to_fire).

    DR-084: deployment_state:abandoned retired; continued is the replacement
    out-of-scope terminal state exercised here.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-continued.md", "claimed",
        deployment_state="continued",
        extra='continued_into: "2026-01-02-successor.md"',
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-continued.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_unconsume_applies_when_claimed_at_by_already_absent(handoff_repo):
    """consumed+in_flight with claimed_at/claimed_by ALREADY absent still completes.

    Review: code-reviewer Finding 2 (P2) — mirrors consume's
    test_consume_completes_partial_state_consumed_not_in_flight. remove_fm_field
    is documented safe on an absent key, but nothing previously asserted that
    this partial-prior-state shape (e.g. a hand-edited record) completes
    cleanly rather than raising.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-already-absent.md", "claimed",
        deployment_state="in_flight",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-already-absent.md")

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "claimed_at") is None
    assert read_fm_field(fm, "claimed_by") is None
    errors = _mod._validate_fm(fm)
    assert errors == [], f"expected no schema errors; got {errors!r}"


def test_unconsume_fails_loud_on_awaiting_gate_source(handoff_repo):
    """unconsume fails loud (exit_code=1, no write) when deployment_state is
    awaiting_gate — out of scope, parity with the shipped/continued tests.

    Review: code-reviewer Finding 3 (nit) — the docstring names
    shipped/continued/awaiting_gate as out-of-scope, but only shipped/continued
    had dedicated fail-loud tests.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-awaiting-gate.md", "claimed",
        deployment_state="awaiting_gate",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-awaiting-gate.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_unconsume_fails_loud_on_multiline_note(handoff_repo):
    """unconsume with a note containing an embedded newline fails loud (no write).

    Review: code-reviewer Finding 5 (nit) — serialize_yaml_scalar's own
    docstring states it does not handle multi-line values; a raw \\n/\\r would
    silently corrupt the single-line park_note: frontmatter field.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-multiline-note.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-multiline-note.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _unconsume_params(abs_path, note="line one\nline two"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"multi-line note must fail loud; got {result!r}"
    )
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_unconsume_with_note_stamps_park_note(handoff_repo):
    """unconsume with a non-empty note param stamps park_note: in frontmatter;
    body is unchanged."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-note.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-note.md")
    original_body = open(abs_path, encoding="utf-8").read().split("---", 2)[2]

    result = _run(_handler(
        _unconsume_params(abs_path, note="decided not to proceed"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True

    content = open(abs_path, encoding="utf-8").read()
    fm = _read_fm(abs_path)
    assert "decided not to proceed" in (read_fm_field(fm, "park_note") or "")
    body = content.split("---", 2)[2]
    assert body == original_body, "body must be unchanged — park_note is frontmatter-only"


def test_unconsume_without_note_writes_no_park_note_key(handoff_repo):
    """unconsume with an absent/empty note writes no park_note key at all."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-no-note.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-no-note.md")
    original_body = open(abs_path, encoding="utf-8").read().split("---", 2)[2]

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True

    content = open(abs_path, encoding="utf-8").read()
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "park_note") is None
    assert "park_note" not in fm
    body = content.split("---", 2)[2]
    assert body == original_body


def test_unconsume_output_passes_schema_validation(handoff_repo):
    """The active+ready_to_fire+no-claimed_by post-mutation shape is schema-valid
    AND matches the concrete expected post-state independent of re-calling
    _validate_fm.

    Review: code-reviewer Finding 4 (nit) — re-invoking _validate_fm against the
    on-disk output only proves the validator agrees with itself (it already
    gated the write). Strengthened to assert the concrete expected fields
    directly, independent of the validator.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-schema.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-schema.md")

    result = _run(_handler(
        _unconsume_params(abs_path),        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, (
        f"schema validation must accept active+ready_to_fire+no-claimed_by; "
        f"got {result!r}"
    )
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    # Concrete expected post-state, asserted independent of _validate_fm.
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "claimed_at") is None, "claimed_at must be absent"
    assert read_fm_field(fm, "claimed_by") is None, "claimed_by must be absent"
    assert "claimed_at" not in fm
    assert "claimed_by" not in fm

    errors = _mod._validate_fm(fm)
    assert errors == [], f"expected no schema errors; got {errors!r}"


def test_unconsume_rejects_traversal_path(handoff_repo):
    """unconsume also rejects traversal paths — same containment guard."""
    secret = handoff_repo.root / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        _unconsume_params("state/handoffs/../../secret.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original


def test_unconsume_lock_timeout_returns_error(handoff_repo, monkeypatch):
    """unconsume maps LockTimeout to exit_code=1 error result; no write is attempted."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-timeout.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-timeout.md")
    original = open(abs_path, encoding="utf-8").read()

    monkeypatch.setattr(_mod, "locked_rmw", _raise_lock_timeout)

    result = _run(_handler(
        _unconsume_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    error_msg = result.get("error", "")
    assert "timeout" in error_msg.lower() or "lock" in error_msg.lower()
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# (l0) unconsume — completeness check: refuse when the governing plan is
#      already stamped implemented (C7, docs/plans/2026-08-04-terminal-
#      state-propagation-join-keys.md). Belt-and-braces under R1: catches a
#      handoff dropped after its plan reached implemented, whether or not the
#      C6 stamp-time cascade already fired.
# ---------------------------------------------------------------------------


def _seed_plan(repo_root: Path, name: str, deliverable_id: str, status: str, title: str = "Test Plan") -> Path:
    """Write a minimal docs/plans/<name>.md with the given deliverable_id + status.

    Read-only surface for _unclaim's completeness check — never git-committed
    (the scan reads straight off disk, not off HEAD).
    """
    path = repo_root / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f'title: "{title}"\n'
        f"status: {status}\n"
        f'deliverable_id: "{deliverable_id}"\n'
        "---\n\n# Plan\n\nBody.\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def test_unconsume_refuses_when_governing_plan_implemented(handoff_repo):
    """unconsume refuses (no write) when the handoff's deliverable_id joins to a
    plan stamped status: implemented — the C7 completeness check. The refusal
    names the plan so an operator has a next move."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-plan-implemented.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
        extra='deliverable_id: "dlv-test-c7-abc123"',
    )
    _seed_plan(
        handoff_repo.root, "2026-01-01-c7-plan.md", "dlv-test-c7-abc123", "implemented",
        title="C7 Test Governing Plan",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-plan-implemented.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _unconsume_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    error_msg = result.get("error", "")
    assert "C7 Test Governing Plan" in error_msg
    assert "2026-01-01-c7-plan.md" in error_msg
    assert open(abs_path, encoding="utf-8").read() == original, "no write must occur on refusal"


def test_unconsume_proceeds_when_governing_plan_not_implemented(handoff_repo):
    """unconsume still applies normally when the governing plan exists but is not
    stamped implemented — the completeness check is a narrow, named gate, not a
    general block on every deliverable_id-carrying handoff."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-plan-approved.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
        extra='deliverable_id: "dlv-test-c7-def456"',
    )
    _seed_plan(handoff_repo.root, "2026-01-01-c7-plan-approved.md", "dlv-test-c7-def456", "approved")
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-plan-approved.md")

    result = _run(_handler(
        _unconsume_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"


def test_unconsume_proceeds_when_no_deliverable_id(handoff_repo):
    """unconsume applies normally when the handoff carries no deliverable_id at
    all — nothing to join, so the completeness check is a no-op."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-no-deliverable-id.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-no-deliverable-id.md")

    result = _run(_handler(
        _unconsume_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True


def test_unconsume_refuses_even_for_unrelated_plan_with_matching_deliverable_id_only(handoff_repo):
    """The join is exact-string deliverable_id match, not fuzzy title/path matching —
    a plan whose deliverable_id merely SHARES A PREFIX must not false-match."""
    handoff_repo.seed_handoff(
        "2026-01-01-unconsume-prefix.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=_SESSION_ID,
        extra='deliverable_id: "dlv-test-c7-prefix"',
    )
    _seed_plan(
        handoff_repo.root, "2026-01-01-c7-plan-prefix-extra.md", "dlv-test-c7-prefix-extra", "implemented",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-unconsume-prefix.md")

    result = _run(_handler(
        _unconsume_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unrelated plan (prefix-only match) must not refuse; result={result!r}"
    assert result["applied"] is True


# ---------------------------------------------------------------------------
# (l) gate-recheck — cleared-flip, bare-stamp, idempotent no-op, fail-loud,
#     schema-reject on malformed
# ---------------------------------------------------------------------------


def test_gate_recheck_cleared_flips_and_strips_dependency(handoff_repo):
    """gate-recheck --cleared flips awaiting_gate→ready_to_fire, strips
    gate_dependency ENTIRELY (key removed, not blanked), and stamps last_gate_recheck.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-gate-cleared.md", "open",
        deployment_state="awaiting_gate",
        extra="gate_dependency: pcore-99 landing",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-cleared.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "gate_dependency") is None, (
        "gate_dependency must be STRIPPED entirely (key removed), not blanked"
    )
    assert "gate_dependency" not in fm, "gate_dependency key must not appear on disk at all"
    assert _AT_DATE in (read_fm_field(fm, "last_gate_recheck") or "")
    # C8/AC11: the prose is RETIRED, not destroyed -- it survives in blocking_notes.
    assert "pcore-99 landing" in (read_fm_field(fm, "blocking_notes") or "")


def test_gate_recheck_bare_stamps_only(handoff_repo):
    """gate-recheck WITHOUT --cleared stamps last_gate_recheck only; deployment_state
    and gate_dependency are untouched."""
    handoff_repo.seed_handoff(
        "2026-01-01-gate-bare.md", "open",
        deployment_state="awaiting_gate",
        extra="gate_dependency: pcore-99 landing",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-bare.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "awaiting_gate", (
        "bare gate-recheck must NOT change deployment_state"
    )
    assert read_fm_field(fm, "gate_dependency") is not None, (
        "bare gate-recheck must NOT strip gate_dependency"
    )
    assert _AT_DATE in (read_fm_field(fm, "last_gate_recheck") or "")


def test_gate_recheck_idempotent_when_cleared_and_already_ready_to_fire(handoff_repo):
    """gate-recheck --cleared is a no-op when deployment_state is already ready_to_fire."""
    handoff_repo.seed_handoff(
        "2026-01-01-gate-noop.md", "open",
        deployment_state="ready_to_fire",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-noop.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_gate_recheck_fails_loud_on_non_awaiting_gate_source(handoff_repo):
    """gate-recheck fails loud (non-zero exit, no write) when deployment_state is
    NOT awaiting_gate (and not already ready_to_fire with --cleared) — gate-recheck
    is defined ONLY as the awaiting_gate re-check/clear transition.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-gate-fail.md", "claimed",
        deployment_state="in_flight",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-fail.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] != 0, (
        f"gate-recheck on an in_flight handoff must fail loud; got {result!r}"
    )
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_gate_recheck_missing_at_returns_error(handoff_repo):
    """gate-recheck without 'at' returns exit_code=1 and does not write."""
    handoff_repo.seed_handoff(
        "2026-01-01-gate-no-at.md", "open",
        deployment_state="awaiting_gate",
        extra="gate_dependency: pcore-99 landing",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-no-at.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        {"verb": "gate-recheck", "handoff_path": abs_path, "cleared": True},
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_gate_recheck_schema_reject_on_malformed_leaves_file_unchanged(handoff_repo):
    """gate-recheck --cleared on a handoff with an over-cap summary is rejected by the
    post-mutation schema validation gate; the on-disk file is left unmodified
    (fail-closed — no write on schema error, same discipline as consume/supersede/ship).
    """
    long_summary = "C" * 141
    handoff_repo.seed_handoff(
        "2026-06-01-gate-malformed.md", "open",
        created="2026-06-01",
        branch="work/test/2026-06-01",
        deployment_state="awaiting_gate",
        category="infra",
        summary=long_summary,
        extra="gate_dependency: pcore-99 landing",
    )
    abs_path = handoff_repo.abs_path("2026-06-01-gate-malformed.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"over-cap summary must block the gate-recheck write; got {result!r}"
    )
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original, (
        "schema-reject must leave the on-disk file unchanged"
    )


def test_repark_lock_timeout_returns_error(handoff_repo, monkeypatch):
    """repark maps LockTimeout to exit_code=1 error result; no write is attempted."""
    handoff_repo.seed_handoff(
        "2026-01-01-repark-timeout.md", "claimed",
        deployment_state="in_flight",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-repark-timeout.md")
    original = open(abs_path, encoding="utf-8").read()

    monkeypatch.setattr(_mod, "locked_rmw", _raise_lock_timeout)

    result = _run(_handler(
        _repark_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    error_msg = result.get("error", "")
    assert "timeout" in error_msg.lower() or "lock" in error_msg.lower()
    assert open(abs_path, encoding="utf-8").read() == original


def test_gate_recheck_lock_timeout_returns_error(handoff_repo, monkeypatch):
    """gate-recheck maps LockTimeout to exit_code=1 error result; no write is attempted."""
    handoff_repo.seed_handoff(
        "2026-01-01-gate-timeout.md", "open",
        deployment_state="awaiting_gate",
        extra="gate_dependency: pcore-99 landing",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-timeout.md")
    original = open(abs_path, encoding="utf-8").read()

    monkeypatch.setattr(_mod, "locked_rmw", _raise_lock_timeout)

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    error_msg = result.get("error", "")
    assert "timeout" in error_msg.lower() or "lock" in error_msg.lower()
    assert open(abs_path, encoding="utf-8").read() == original


def test_repark_rejects_traversal_path(handoff_repo):
    """repark also rejects traversal paths — same containment guard."""
    secret = handoff_repo.root / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        _repark_params("state/handoffs/../../secret.md"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original


def test_gate_recheck_rejects_traversal_path(handoff_repo):
    """gate-recheck also rejects traversal paths — same containment guard."""
    secret = handoff_repo.root / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        _gate_recheck_params("state/handoffs/../../secret.md", at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (l2) gate-recheck — gate_evidence live re-resolution + per-leg persistence (C4)
#
# docs/plans/2026-07-26-structured-sibling-evidence-gates.md § C4 (AC2/AC9).
# ---------------------------------------------------------------------------


_GATE_EVIDENCE_REPO_ID = "gate-evidence-fixture-repo"


def _register_gate_evidence_repo(monkeypatch, repo_path):
    """Point `repo: gate-evidence-fixture-repo` at a tmp_path repo via the
    sanctioned `MACHINE_LOCAL_REPOS_<ID>` test hook (sibling_fact's own
    resolution ladder) — never a registry file write."""
    env_key = "MACHINE_LOCAL_REPOS_" + _GATE_EVIDENCE_REPO_ID.upper()
    monkeypatch.setenv(env_key, str(repo_path))


def test_gate_recheck_bare_persists_per_leg_gate_evidence_results(handoff_repo, monkeypatch, tmp_path):
    """A bare (non-cleared) gate-recheck re-resolves every gate_evidence leg
    LIVE and persists PER-LEG results (AC9) — not a last_gate_recheck-shaped
    summary. One file-exists leg (satisfied) + one human leg (permanently
    indeterminate) together reduce the whole block to indeterminate."""
    sibling_repo = tmp_path / "sibling"
    sibling_repo.mkdir()
    (sibling_repo / "marker.txt").write_text("present\n", encoding="utf-8")
    _register_gate_evidence_repo(monkeypatch, sibling_repo)

    extra = (
        "gate_dependency: sibling marker exists\n"
        "gate_evidence:\n"
        "  covers_prose: true\n"
        "  legs:\n"
        "    - leg_id: leg-file\n"
        "      kind: file-exists\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        "      ref: marker.txt\n"
        "      expected: true\n"
        "      note: marker file presence\n"
        "    - leg_id: leg-human\n"
        "      kind: human\n"
        "      reason: PM must ratify the window"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-evidence-bare.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-evidence-bare.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "awaiting_gate", (
        "bare gate-recheck must never touch deployment_state (F8 write-scope pin)"
    )
    assert "gate_evidence_results:" in fm
    assert "status: indeterminate" in fm, (
        "a human leg makes the whole AND-reduce indeterminate regardless of the "
        "satisfied file-exists leg"
    )
    assert "leg_id: leg-file" in fm and "leg_id: leg-human" in fm, (
        "PER-LEG results required (AC9) -- both legs must be individually named"
    )
    assert "status: satisfied" in fm, "leg-file's own per-leg status must be recorded"
    assert "status: indeterminate" in fm.split("legs:")[-1], (
        "leg-human's own per-leg status must be recorded"
    )


def test_gate_recheck_cleared_refused_when_gate_evidence_not_freed(handoff_repo, monkeypatch, tmp_path):
    """--cleared is REFUSED (MutateAbort, no write) when a gate_evidence block
    is present but does not live-resolve to 'freed' -- the act-time
    re-verification guard inherited from gate-cascade-clear (F0 precedent):
    a `cleared` claim is never trusted blindly."""
    sibling_repo = tmp_path / "sibling-unsatisfied"
    sibling_repo.mkdir()
    # marker.txt deliberately absent -- file-exists leg observes False, expected True.
    _register_gate_evidence_repo(monkeypatch, sibling_repo)

    extra = (
        "gate_evidence:\n"
        "  covers_prose: false\n"
        "  legs:\n"
        "    - leg_id: leg-file\n"
        "      kind: file-exists\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        "      ref: marker.txt\n"
        "      expected: true\n"
        "      note: marker file presence"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-evidence-unsatisfied.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-evidence-unsatisfied.md")
    original_content = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, f"expected refusal; result={result!r}"
    assert result["applied"] is False
    assert "freed" in result["error"]
    assert open(abs_path, encoding="utf-8").read() == original_content, (
        "a refused --cleared request must leave the file byte-identical -- "
        "no partial write, no per-leg results persisted either"
    )


def test_gate_recheck_cleared_proceeds_when_gate_evidence_freed(handoff_repo, monkeypatch, tmp_path):
    """--cleared proceeds (flip + gate_dependency retirement) when live
    re-resolution reduces the whole gate_evidence block to 'freed'.

    C7 (AC10): gate_evidence and its just-computed gate_evidence_results are
    BOTH lifecycle-stripped in the same write once the flip to ready_to_fire
    lands — _cf_ready_to_fire_no_gate_evidence forbids gate_evidence surviving
    onto a ready_to_fire record, the same way gate_dependency is forbidden.
    The freed per-leg results are real (this asserts the live re-resolution
    ran and reduced to 'freed' via the exit_code/applied/deployment_state
    assertions below) but do not outlive the claim they resolved."""
    sibling_repo = tmp_path / "sibling-satisfied"
    sibling_repo.mkdir()
    (sibling_repo / "marker.txt").write_text("present\n", encoding="utf-8")
    _register_gate_evidence_repo(monkeypatch, sibling_repo)

    extra = (
        "gate_dependency: sibling marker exists\n"
        "gate_evidence:\n"
        "  covers_prose: true\n"
        "  legs:\n"
        "    - leg_id: leg-file\n"
        "      kind: file-exists\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        "      ref: marker.txt\n"
        "      expected: true\n"
        "      note: marker file presence"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-evidence-freed.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-evidence-freed.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "gate_dependency") is None
    assert "gate_evidence:" not in fm
    assert "gate_evidence_results:" not in fm
    assert "leg_id: leg-file" not in fm


def test_gate_recheck_ignores_gate_evidence_when_absent(handoff_repo):
    """A handoff carrying no gate_evidence: block is unaffected by C4 -- byte
    behaviour matches the pre-C4 verb exactly (no new field appears)."""
    handoff_repo.seed_handoff(
        "2026-01-01-gate-no-evidence.md", "open",
        deployment_state="awaiting_gate",
        extra="gate_dependency: pcore-99 landing",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-no-evidence.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert "gate_evidence_results" not in fm


# ---------------------------------------------------------------------------
# (l2a) gate-recheck — frontmatter-field / commit-ancestor END-TO-END (AC7)
#
# docs/plans/2026-07-26-structured-sibling-evidence-gates.md § AC7.
#
# The gap these close: `_sibling_fact_leg_for` is the ONLY site that decodes
# the schema's composite `ref` separators ('<path>#<field>',
# '<commit-ish>@<target-ref>'), and only its `file-exists` branch had
# end-to-end coverage. A partition read off the wrong side of the separator
# resolves `read_ok: False` -> `indeterminate` — a gate that quietly never
# frees, with nothing failing anywhere. Both remaining branches are driven
# here against a REAL git sibling on disk, in both polarities, so an inverted
# partition cannot pass: a swapped '#' makes the frontmatter leg indeterminate
# (unreadable path) rather than unsatisfied, and a swapped '@' inverts an
# ancestry answer that is deliberately asymmetric in the fixture.
#
# The load-bearing assertion throughout is AC7's: the persisted per-leg
# statuses come from the SIBLING'S DISK, never from what the record's own
# prose claims — every fixture below authors a `gate_dependency` that
# contradicts the disk.
# ---------------------------------------------------------------------------


#: Git identity is pinned per-invocation with `-c` rather than inherited from
#: the machine's global config — the suite-root conftest quarantines HOME, so
#: there is no ~/.gitconfig to fall back on and an unidentified `git commit`
#: dies with a confusing downstream symptom.
_FIXTURE_GIT_IDENTITY = (
    "-c", "user.email=gate-evidence@claude-klabauter.test",
    "-c", "user.name=Gate Evidence Fixture",
)

#: Windows-first-class subprocess posture (plan § subprocess posture): the
#: `getattr` form resolves to CREATE_NO_WINDOW on Windows and 0 (a no-op
#: `creationflags`) everywhere else, so it is safe to pass unconditionally.
_FIXTURE_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _fixture_git(repo: Path, *args: str) -> str:
    """Run one git command inside a fixture repo, returning trimmed stdout.

    Re-established LOCALLY rather than hoisted to a shared conftest: the only
    other copy (`coordinator_core/tests/test_sibling_fact.py`'s module-private
    `_git`/`_make_commit`) belongs to a different test package and is
    verified-correct as-is against the snake_case primitive vocabulary, so
    hoisting would mean editing that file to consume a fixture with exactly
    one other caller. Two four-line helpers in two packages beat a
    cross-package conftest surface that exists to serve one consumer.
    """
    result = subprocess.run(
        ["git", *_FIXTURE_GIT_IDENTITY, *args],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        check=True,
        stdin=subprocess.DEVNULL,
        **_FIXTURE_NO_CONSOLE,
    )
    return (result.stdout or "").strip()


def _seed_sibling_git_repo(
    tmp_path: Path, name: str, plan_status: str
) -> Tuple[Path, str, str]:
    """Create a real git sibling carrying `plan.md` (frontmatter
    `status: <plan_status>`) and TWO commits.

    Returns `(repo, ancestor_sha, head_sha)`. The two commits make ancestry
    deliberately ASYMMETRIC — `ancestor_sha` is an ancestor of `head_sha` and
    never the reverse — which is what lets a `commit-ancestor` leg's '@'
    partition polarity be asserted rather than assumed.
    """
    repo = tmp_path / name
    repo.mkdir()
    _fixture_git(repo, "init", "-b", "main")
    _fixture_git(repo, "config", "commit.gpgsign", "false")

    (repo / "plan.md").write_text(
        f"---\ntitle: sibling plan\nstatus: {plan_status}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    _fixture_git(repo, "add", "plan.md")
    _fixture_git(repo, "commit", "-m", "add sibling plan")
    ancestor_sha = _fixture_git(repo, "rev-parse", "HEAD")

    _fixture_git(repo, "commit", "--allow-empty", "-m", "later sibling work")
    head_sha = _fixture_git(repo, "rev-parse", "HEAD")

    return repo, ancestor_sha, head_sha


def _read_gate_evidence_results(path_str: str) -> dict:
    """Parse the persisted `gate_evidence_results:` block off disk."""
    parsed = yaml.safe_load(_read_fm(path_str)) or {}
    results = parsed.get("gate_evidence_results")
    assert isinstance(results, dict), (
        f"gate_evidence_results absent or malformed in {path_str}: {parsed!r}"
    )
    return results


def _leg_result(results: dict, leg_id: str) -> dict:
    legs = results.get("legs")
    legs = legs if isinstance(legs, list) else []
    for leg in legs:
        if isinstance(leg, dict) and leg.get("leg_id") == leg_id:
            return leg
    raise AssertionError(f"no per-leg result for {leg_id!r} in {results!r}")


def test_gate_recheck_frontmatter_field_and_commit_ancestor_read_sibling_disk(
    handoff_repo, monkeypatch, tmp_path
):
    """AC7: per-leg statuses come from the sibling's DISK, not the record's prose.

    The handoff's own `gate_dependency` asserts the sibling plan shipped. The
    sibling's `plan.md` on disk says `status: draft`. The persisted
    `frontmatter-field` leg must report UNSATISFIED against the disk, and the
    `commit-ancestor` leg SATISFIED against real ancestry — the prose is never
    consulted.

    Also pins both composite-`ref` partitions: a '#' read off the wrong side
    would make the frontmatter leg `indeterminate` (path `status` unreadable),
    not `unsatisfied` with an observed value of `draft`.
    """
    sibling_repo, ancestor_sha, head_sha = _seed_sibling_git_repo(
        tmp_path, "sibling-draft", plan_status="draft"
    )
    _register_gate_evidence_repo(monkeypatch, sibling_repo)

    extra = (
        'gate_dependency: "sibling plan shipped weeks ago, nothing left to wait on"\n'
        "gate_evidence:\n"
        "  covers_prose: true\n"
        "  legs:\n"
        "    - leg_id: leg-fm\n"
        "      kind: frontmatter-field\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        "      ref: plan.md#status\n"
        "      expected: shipped\n"
        "    - leg_id: leg-ancestor\n"
        "      kind: commit-ancestor\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        f"      ref: {ancestor_sha}@{head_sha}\n"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-evidence-disk-truth.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-evidence-disk-truth.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    results = _read_gate_evidence_results(abs_path)
    assert results["status"] == "still-blocked", (
        "one unsatisfied leg must AND-reduce the block to still-blocked"
    )

    fm_leg = _leg_result(results, "leg-fm")
    assert fm_leg["status"] == "unsatisfied", (
        "the sibling's plan.md says status: draft while the record's prose claims "
        f"shipped — disk wins; leg={fm_leg!r}"
    )
    assert "draft" in str(fm_leg["reason"]), (
        "the observed value must be the one READ from the sibling's frontmatter "
        f"(status: draft), proving 'plan.md#status' split path/field correctly; leg={fm_leg!r}"
    )

    ancestor_leg = _leg_result(results, "leg-ancestor")
    assert ancestor_leg["status"] == "satisfied", (
        f"{ancestor_sha} IS an ancestor of {head_sha} in the fixture repo; "
        f"leg={ancestor_leg!r}"
    )

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "awaiting_gate", (
        "bare gate-recheck must never touch deployment_state (F8 write-scope pin)"
    )
    assert read_fm_field(fm, "gate_dependency") is not None, (
        "the contradicted prose is left standing — the evidence block is what resolved, "
        "and a bare recheck retires nothing"
    )


def test_gate_recheck_frontmatter_field_satisfied_when_sibling_disk_agrees(
    handoff_repo, monkeypatch, tmp_path
):
    """The other polarity of the same '#' partition: when the sibling's own
    frontmatter matches `expected`, the leg is SATISFIED and, with every other
    leg satisfied, the whole block reduces to `freed`.

    Both polarities are needed: a partition bug that always yields `None`
    would still produce an unsatisfied leg in the test above, and could hide
    there. It cannot produce a satisfied leg here.
    """
    sibling_repo, ancestor_sha, head_sha = _seed_sibling_git_repo(
        tmp_path, "sibling-shipped", plan_status="shipped"
    )
    _register_gate_evidence_repo(monkeypatch, sibling_repo)

    extra = (
        'gate_dependency: "sibling plan is still a draft"\n'
        "gate_evidence:\n"
        "  covers_prose: true\n"
        "  legs:\n"
        "    - leg_id: leg-fm\n"
        "      kind: frontmatter-field\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        "      ref: plan.md#status\n"
        "      expected: shipped\n"
        "    - leg_id: leg-ancestor\n"
        "      kind: commit-ancestor\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        f"      ref: {ancestor_sha}@{head_sha}\n"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-evidence-disk-agrees.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-evidence-disk-agrees.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"

    results = _read_gate_evidence_results(abs_path)
    assert results["status"] == "freed", (
        "both legs satisfied against the sibling's disk must reduce to freed, even "
        f"though the record's prose says the sibling is still a draft; results={results!r}"
    )
    assert _leg_result(results, "leg-fm")["status"] == "satisfied"
    assert _leg_result(results, "leg-ancestor")["status"] == "satisfied"


def test_gate_recheck_commit_ancestor_ref_polarity_is_not_symmetric(
    handoff_repo, monkeypatch, tmp_path
):
    """`'<commit-ish>@<target-ref>'` is directional: with the two ends swapped,
    the SAME fixture answers `unsatisfied` (a real, asked-and-answered
    negative — `read_ok` is True), never `indeterminate`.

    This is the teeth on the '@' partition. If `_sibling_fact_leg_for` read
    the ends the other way round, the swapped leg here would come back
    satisfied and the correctly-ordered leg in the test above unsatisfied.
    """
    sibling_repo, ancestor_sha, head_sha = _seed_sibling_git_repo(
        tmp_path, "sibling-polarity", plan_status="draft"
    )
    _register_gate_evidence_repo(monkeypatch, sibling_repo)

    extra = (
        'gate_dependency: "successor commit is already merged behind the base"\n'
        "gate_evidence:\n"
        "  covers_prose: false\n"
        "  legs:\n"
        "    - leg_id: leg-reversed\n"
        "      kind: commit-ancestor\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        f"      ref: {head_sha}@{ancestor_sha}\n"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-evidence-polarity.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-evidence-polarity.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"

    results = _read_gate_evidence_results(abs_path)
    leg = _leg_result(results, "leg-reversed")
    assert leg["status"] == "unsatisfied", (
        f"HEAD {head_sha} is NOT an ancestor of {ancestor_sha}; this must be a real "
        f"negative, and specifically not 'indeterminate' (which would mean the "
        f"ancestry question was never actually asked); leg={leg!r}"
    )
    assert results["status"] == "still-blocked"


def test_sibling_fact_leg_for_rejects_unrecognised_kind():
    """An unrecognised kind raises rather than falling through into a
    `commit_ancestor` request.

    Before the explicit `commit-ancestor` guard, `commit-ancestor` was this
    function's bare `else` tail: any other kind was silently '@'-partitioned
    into a commit-ancestry probe that could only ever resolve
    `read_ok: False` -> `indeterminate`, i.e. a gate that never frees with
    nothing anywhere reporting a fault.
    """
    with pytest.raises(ValueError) as excinfo:
        _mod._sibling_fact_leg_for({
            "leg_id": "leg-c6",
            "kind": "commit-sha",
            "repo": _GATE_EVIDENCE_REPO_ID,
            "ref": "deadbeef",
        })

    message = str(excinfo.value)
    assert "commit-sha" in message and "leg-c6" in message, (
        f"the failure must name the offending leg and kind; got: {message}"
    )


def test_reresolve_gate_evidence_leg_never_reaches_projector_with_unknown_kind():
    """The caller's own kind guard keeps the fail-loud projector unreachable
    for kinds gate-recheck has no live re-verifier for: those legs are
    reported honestly `read_ok: False` (-> indeterminate downstream), not
    raised on and not silently resolved."""
    leg = _mod._reresolve_gate_evidence_leg(
        {"leg_id": "leg-c6", "kind": "commit-sha", "repo": _GATE_EVIDENCE_REPO_ID, "ref": "deadbeef"},
        _AT_DATE,
    )

    assert leg["read_ok"] is False
    assert "commit-sha" in str(leg["error"])


# ---------------------------------------------------------------------------
# (l2b) gate_evidence RETIREMENT into blocking_notes (AC9)
#
# docs/plans/2026-07-26-structured-sibling-evidence-gates.md § AC9/AC10.
# The defect closed here: an evidence-driven clear wrote gate_evidence_results
# and destroyed it in the SAME write, while gate_cleared_by (SHA provenance)
# populates only on the gate-cascade-clear path -- leaving a bare
# last_gate_recheck: date and nothing about WHICH leg resolved. Mirrors
# _retire_gate_dependency's own guarantees (no-op-when-absent,
# never-overwrite-existing-prose) because both share its append half
# (primitives._append_blocking_note).
# ---------------------------------------------------------------------------


def test_gate_recheck_cleared_retires_gate_evidence_into_blocking_notes(
    handoff_repo, monkeypatch, tmp_path
):
    """AC9: after an evidence-driven clear the frontmatter still names WHICH
    leg resolved and how -- leg_id + kind + status survive into blocking_notes
    even though gate_evidence/gate_evidence_results are both stripped by the
    same write (AC10)."""
    sibling_repo = tmp_path / "sibling-retire"
    sibling_repo.mkdir()
    (sibling_repo / "marker.txt").write_text("present\n", encoding="utf-8")
    _register_gate_evidence_repo(monkeypatch, sibling_repo)

    extra = (
        "gate_dependency: sibling marker exists\n"
        "gate_evidence:\n"
        "  covers_prose: true\n"
        "  legs:\n"
        "    - leg_id: leg-file\n"
        "      kind: file-exists\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        "      ref: marker.txt\n"
        "      expected: true\n"
        "      note: marker file presence"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-evidence-retired.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-evidence-retired.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    # AC10 still holds -- both keys are gone from the ready_to_fire record.
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert "gate_evidence:" not in fm
    assert "gate_evidence_results:" not in fm

    notes = read_fm_field(fm, "blocking_notes") or ""
    assert "leg-file" in notes, "AC9: the retired note must name WHICH leg resolved"
    assert "file-exists" in notes, "AC9: the leg's kind must survive"
    assert "satisfied" in notes, "AC9: the leg's own resolved status must survive"
    assert "freed" in notes, "the whole-block reduce status must survive"
    assert _AT_DATE in notes, "checked_at must survive so the note is not itself vacuous"
    # gate_dependency's own retirement (C8) is not displaced by ours.
    assert "sibling marker exists" in notes


def test_gate_recheck_cleared_retirement_never_overwrites_blocking_notes(
    handoff_repo, monkeypatch, tmp_path
):
    """Mirrors _retire_gate_dependency's never-overwrite guarantee: pre-existing
    advisory prose unrelated to this gate survives the retirement, joined rather
    than replaced."""
    sibling_repo = tmp_path / "sibling-append"
    sibling_repo.mkdir()
    (sibling_repo / "marker.txt").write_text("present\n", encoding="utf-8")
    _register_gate_evidence_repo(monkeypatch, sibling_repo)

    extra = (
        "blocking_notes: unrelated advisory prose\n"
        "gate_evidence:\n"
        "  covers_prose: false\n"
        "  legs:\n"
        "    - leg_id: leg-file\n"
        "      kind: file-exists\n"
        f"      repo: {_GATE_EVIDENCE_REPO_ID}\n"
        "      ref: marker.txt\n"
        "      expected: true\n"
        "      note: marker file presence"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-evidence-append.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-evidence-append.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"

    fm = _read_fm(abs_path)
    notes = read_fm_field(fm, "blocking_notes") or ""
    assert "unrelated advisory prose" in notes, "existing prose must never be overwritten"
    assert "leg-file" in notes
    assert notes.count("blocking_notes") == 0
    assert fm.count("blocking_notes:") == 1, "never mint a second blocking_notes key"


def test_gate_evidence_retirement_is_noop_when_absent():
    """No gate_evidence and no gate_evidence_results -> byte-identical
    frontmatter, no empty blocking_notes minted. Mirrors
    _retire_gate_dependency's no-op-when-absent contract, so the retire call is
    safe at a site that may or may not carry evidence."""
    fm = "status: open\ndeployment_state: awaiting_gate\ngate_dependency: waiting\n"
    assert _mod._retire_gate_evidence(fm) == fm


def test_gate_evidence_retirement_summary_is_single_line():
    """blocking_notes is a single-line YAML scalar, so a multi-line (or
    CRLF-authored) leg reason must be collapsed, never carried through as a
    newline that would corrupt the document."""
    note = _mod._render_gate_evidence_retirement(
        None,
        {
            "status": "freed",
            "checked_at": "2026-01-02",
            "legs": [
                {
                    "leg_id": "leg-crlf",
                    "kind": "frontmatter-field",
                    "status": "satisfied",
                    "reason": "line one\r\nline two\tand more",
                }
            ],
        },
    )
    assert "\n" not in note and "\r" not in note
    assert "leg-crlf" in note and "frontmatter-field" in note and "satisfied" in note
    assert "line one line two and more" in note


def test_gate_evidence_retirement_never_strips_a_malformed_block_silently():
    """A present-but-malformed `gate_evidence:` (neither key parses to a dict)
    is still stripped, so it must still leave a paper trail.

    Returning '' here would hand `_strip_gate_evidence` an on-disk block to
    destroy with nothing recorded — a milder instance of the vacuity AC9
    exists to close. Only the both-keys-absent case may render nothing, and
    that case destroys nothing either.
    """
    assert _mod._render_gate_evidence_retirement(None, None) == "", (
        "nothing on disk to retire must stay a true no-op"
    )

    note = _mod._render_gate_evidence_retirement("just a prose sentence", None)
    assert "malformed" in note, f"a malformed block must be named, not silently dropped; got {note!r}"

    fm = (
        'title: "Malformed evidence"\n'
        "deployment_state: awaiting_gate\n"
        "gate_evidence: just a prose sentence\n"
    )
    retired = _mod._retire_gate_evidence(fm)
    parsed = yaml.safe_load(retired)

    assert "gate_evidence" not in parsed, "the malformed block is still stripped"
    assert "malformed" in parsed["blocking_notes"], (
        f"the strip must be recorded in blocking_notes; got {parsed!r}"
    )


def test_gate_evidence_retirement_note_round_trips_yaml_structural_characters():
    """A leg `reason` carrying YAML structural characters (`#`, `:`, a literal
    `'`) survives the whole retirement path — render, append, serialize — and
    re-parses as ONE string.

    The single-line test above stops at the renderer's return value; the
    quoting that actually protects the document lives downstream in
    `serialize_yaml_scalar`, and until now was exercised only for
    `gate_dependency` values, never for a `gate_evidence`-sourced note. This
    is the missing regression net, not a known defect.
    """
    hostile_reason = "status: shipped # not a comment, it's a value"

    fm = (
        'title: "Retirement round-trip"\n'
        "deployment_state: awaiting_gate\n"
        "gate_evidence_results:\n"
        "  status: freed\n"
        '  checked_at: "2026-01-02"\n'
        "  legs:\n"
        "    - leg_id: leg-yaml\n"
        "      kind: frontmatter-field\n"
        "      status: satisfied\n"
        f'      reason: "{hostile_reason}"\n'
    )

    retired = _mod._retire_gate_evidence(fm)
    parsed = yaml.safe_load(retired)

    assert "gate_evidence_results" not in parsed
    note = parsed["blocking_notes"]
    assert isinstance(note, str), f"blocking_notes must re-parse as one string; got {note!r}"
    assert "\n" not in note
    assert "leg-yaml" in note
    assert hostile_reason in note, (
        "the reason must survive quoting byte-for-byte -- a '#' swallowed as a comment "
        f"or a ':' re-parsed as a mapping would corrupt it; got {note!r}"
    )
    assert parsed["deployment_state"] == "awaiting_gate", (
        "the rest of the document must still parse correctly around the quoted note"
    )


def test_gate_cascade_clear_flip_retires_not_rechecked_gate_evidence(handoff_repo):
    """The cascade-clear FLIP also destroys gate_evidence, so it retires too --
    but honestly: a record cleared on its blocker-SHA leg was never rechecked,
    so its legs are reported `not-rechecked` rather than implying a resolution
    nothing observed."""
    _seed_blocker(handoff_repo, "blocker-evidence-retire.md", "stub-evr", "shipped")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-flip-evidence-retire.md", "open",
        deployment_state="awaiting_gate",
        extra=(
            _roadmap_extra("gcc-flip-evidence-retire", "['stub-evr']", "stub-evr work must ship")
            + "\ngate_evidence:\n"
            "  covers_prose: true\n"
            "  legs:\n"
            "    - leg_id: leg-human\n"
            "      kind: human\n"
            "      reason: manual check pending"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-flip-evidence-retire.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-evr"], ["c" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert "gate_evidence:" not in fm
    notes = read_fm_field(fm, "blocking_notes") or ""
    assert "leg-human" in notes and "not-rechecked" in notes


def test_repark_does_not_retire_unresolved_gate_evidence(handoff_repo):
    """Per-site decision (AC9): repark is a hand-back, not a clearance -- its
    gate never cleared, so the discarded evidence resolved nothing worth a
    paper trail and no note is accreted."""
    handoff_repo.seed_handoff(
        "2026-01-01-repark-no-retire.md", "claimed",
        deployment_state="in_flight",
        extra=(
            "gate_evidence:\n"
            "  covers_prose: true\n"
            "  legs:\n"
            "    - leg_id: leg-human\n"
            "      kind: human\n"
            "      reason: manual check pending"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-repark-no-retire.md")

    result = _run(_handler(
        _repark_params(abs_path),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"

    fm = _read_fm(abs_path)
    assert "gate_evidence:" not in fm
    assert "not-rechecked" not in fm
    assert "leg-human" not in fm


# ---------------------------------------------------------------------------
# (l3) gate-recheck — offer-shaped surfacing of an unresolved sibling-naming
#      gate (C5, AC3)
#
# docs/plans/2026-07-26-structured-sibling-evidence-gates.md § C5.
# ---------------------------------------------------------------------------


def _register_sibling_repo(monkeypatch, repo_id: str, repo_path):
    """Point `repos.<repo_id>` at a tmp_path repo via the sanctioned
    `MACHINE_LOCAL_REPOS_<ID>` test hook (same hook C4's
    `_register_gate_evidence_repo` uses) -- `registry_get` honours it
    transparently, so this is enough to make `repo_id` read as "registered"
    without touching any registry file."""
    env_key = "MACHINE_LOCAL_REPOS_" + repo_id.upper()
    monkeypatch.setenv(env_key, str(repo_path))


def test_gate_recheck_offers_gate_evidence_when_prose_names_registered_sibling_with_no_evidence(
    handoff_repo, monkeypatch, tmp_path
):
    """A bare gate-recheck whose gate_dependency prose names a registered
    sibling repo, with NO gate_evidence: block at all, surfaces a
    copy-pasteable offer -- never a hard block (exit_code stays 0, applied
    stays True, the write proceeds exactly as it would without C5)."""
    sibling_repo = tmp_path / "example-doctrine-repo-fixture"
    sibling_repo.mkdir()
    _register_sibling_repo(monkeypatch, "example_doctrine_repo", sibling_repo)

    handoff_repo.seed_handoff(
        "2026-01-01-gate-offer-no-evidence.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "example-doctrine-repo finalizes its contract"',
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-offer-no-evidence.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"an unresolved sibling-naming gate must never hard-block; result={result!r}"
    assert result["applied"] is True
    message = result["message"]
    assert "offer:" in message
    assert "example_doctrine_repo" in message
    assert "gate_evidence:" in message
    assert "covers_prose: true" in message
    assert "kind: file-exists" in message, "no commit/field hint in the prose -- defaults to file-exists"

    # Never mutates the record on the author's behalf -- the offer is
    # message-only, the frontmatter itself gets no gate_evidence: block.
    fm = _read_fm(abs_path)
    assert "gate_evidence:" not in fm


def test_gate_recheck_offers_commit_ancestor_kind_when_prose_hints_at_a_commit(
    handoff_repo, monkeypatch, tmp_path
):
    """The offer's suggested `kind:` follows a cheap prose scan -- a
    commit/landed/shipped hint suggests `commit-ancestor` rather than the
    file-exists default."""
    sibling_repo = tmp_path / "example-retrieval-repo-fixture"
    sibling_repo.mkdir()
    _register_sibling_repo(monkeypatch, "example_retrieval_repo", sibling_repo)

    handoff_repo.seed_handoff(
        "2026-01-01-gate-offer-commit-hint.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "example-retrieval-repo lands the ingest commit"',
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-offer-commit-hint.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert "kind: commit-ancestor" in result["message"]
    assert "example_retrieval_repo" in result["message"]


def test_gate_recheck_no_offer_when_gate_evidence_covers_prose(
    handoff_repo, monkeypatch, tmp_path
):
    """No offer when a gate_evidence: block already asserts
    covers_prose: true -- regardless of its LIVE per-leg resolution status
    (this fixture's file-exists leg is unsatisfied). A human already
    authored real evidence; re-offering on every still-blocked recheck is
    exactly the mistrust-shape (fighting the author's eagerness) this chunk
    exists to avoid."""
    sibling_repo = tmp_path / "example-doctrine-repo-covers"
    sibling_repo.mkdir()
    _register_sibling_repo(monkeypatch, "example_doctrine_repo", sibling_repo)

    extra = (
        'gate_dependency: "example-doctrine-repo finalizes its contract"\n'
        "gate_evidence:\n"
        "  covers_prose: true\n"
        "  legs:\n"
        "    - leg_id: leg-1\n"
        "      kind: file-exists\n"
        "      repo: example_doctrine_repo\n"
        "      ref: marker-not-present.txt\n"
        "      expected: true\n"
        "      note: contract finalized marker"
    )
    handoff_repo.seed_handoff(
        "2026-01-01-gate-covers-prose.md", "open",
        deployment_state="awaiting_gate",
        extra=extra,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-covers-prose.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    assert "offer:" not in result["message"]


def test_gate_recheck_no_offer_when_prose_names_no_registered_repo(handoff_repo):
    """No offer when gate_dependency prose names nothing that resolves as a
    registered repo -- e.g. an internal roadmap-item id, not a sibling."""
    handoff_repo.seed_handoff(
        "2026-01-01-gate-offer-no-repo-named.md", "open",
        deployment_state="awaiting_gate",
        extra="gate_dependency: pcore-99 landing",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-offer-no-repo-named.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=False),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert "offer:" not in result["message"]


def test_gate_recheck_no_offer_on_cleared_call(handoff_repo, monkeypatch, tmp_path):
    """The offer is bare-recheck-only -- a `cleared` call that succeeds has
    just retired gate_dependency in the SAME write, so offering to author
    gate_evidence: for prose that no longer exists on disk would be stale
    and confusing. No gate_evidence: block here, so the flip proceeds on the
    caller's trusted claim exactly as pre-C5."""
    sibling_repo = tmp_path / "example-doctrine-repo-cleared"
    sibling_repo.mkdir()
    _register_sibling_repo(monkeypatch, "example_doctrine_repo", sibling_repo)

    handoff_repo.seed_handoff(
        "2026-01-01-gate-offer-cleared.md", "open",
        deployment_state="awaiting_gate",
        extra='gate_dependency: "example-doctrine-repo finalizes its contract"',
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gate-offer-cleared.md")

    result = _run(_handler(
        _gate_recheck_params(abs_path, at=_AT_DATE, cleared=True),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    assert "offer:" not in result["message"]


# ---------------------------------------------------------------------------
# (m) gate-cascade-clear — narrow-or-flip, gate_cleared_by provenance,
#     act-time re-verification, asymmetry fail-loud, full strip on flip
#
# Spec backlink: docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C8
# ---------------------------------------------------------------------------


def _gate_cascade_clear_params(handoff_path: str, blocker_ids: list, blocker_shas: list) -> dict:
    return {
        "verb": "gate-cascade-clear",
        "handoff_path": handoff_path,
        "blocker_ids": blocker_ids,
        "blocker_shas": blocker_shas,
    }


def _seed_blocker(handoff_repo, name: str, stub_id: str, deployment_state: str) -> None:
    """Seed a roadmap-stub blocker handoff with the given stub_id + deployment_state.

    Uses the extra= raw-line hook since HandoffRepo.seed_handoff has no first-class
    stub_id/deployment_state-for-blockers param combination; deployment_state is
    already a first-class kwarg, stub_id is appended via extra.
    """
    handoff_repo.seed_handoff(
        name,
        "open",
        deployment_state=deployment_state,
        extra=f'stub_id: "{stub_id}"',
    )


def _roadmap_extra(stub_id: str, blocked_by_yaml: str, gate_dependency: str = "") -> str:
    """Build the extra= frontmatter block for a spinoff-roadmap dependent handoff.

    kind: spinoff-roadmap requires roadmap_id/stub_id/wave/blocks/blocked_by
    (schema _cf_spinoff_roadmap_requires_graph) — every gate-cascade-clear test
    that exercises a real blocked_by array must satisfy this cross-field rule.
    """
    lines = [
        "kind: spinoff-roadmap",
        'roadmap_id: "rdm-gcc-test"',
        f'stub_id: "{stub_id}"',
        "wave: 1",
        "blocks: []",
        f"blocked_by: {blocked_by_yaml}",
    ]
    if gate_dependency:
        lines.append(f'gate_dependency: "{gate_dependency}"')
    return "\n".join(lines)


def test_gate_cascade_clear_all_shipped_flips_and_strips_dependency(handoff_repo):
    """All blockers shipped → flip to ready_to_fire, gate_cleared_by stamped,
    gate_dependency stripped ENTIRELY (the Staff Engineer F2 — full strip on flip)."""
    _seed_blocker(handoff_repo, "blocker-a.md", "stub-a", "shipped")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-flip.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra("gcc-flip", "['stub-a']", "stub-a work must ship"),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-flip.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-a"], ["a" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "gate_dependency") is None, (
        "flip branch must STRIP gate_dependency entirely, not blank it"
    )
    # C8/AC11: the prose is RETIRED, not destroyed -- it survives in blocking_notes.
    assert "stub-a work must ship" in (read_fm_field(fm, "blocking_notes") or "")
    assert "a" * 40 in (read_fm_field(fm, "gate_cleared_by") or "")
    assert read_fm_field(fm, "blocked_by") in ("[]", "", None) or "stub-a" not in (
        read_fm_field(fm, "blocked_by") or ""
    )


def test_gate_cascade_clear_flip_strips_gate_evidence(handoff_repo):
    """C7 (AC10): the FLIP branch also strips gate_evidence entirely (nested-
    block REMOVE), matching its gate_dependency full-strip-on-flip treatment."""
    _seed_blocker(handoff_repo, "blocker-evidence.md", "stub-ev", "shipped")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-flip-evidence.md", "open",
        deployment_state="awaiting_gate",
        extra=(
            _roadmap_extra("gcc-flip-evidence", "['stub-ev']", "stub-ev work must ship")
            + "\ngate_evidence:\n"
            "  covers_prose: true\n"
            "  legs:\n"
            "    - kind: human\n"
            "      reason: manual check pending"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-flip-evidence.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-ev"], ["a" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert "gate_evidence:" not in fm, "gate_evidence key must not appear on disk at all"
    # No ORPHANED continuation lines: the indented `legs:` / `- kind:` /
    # `reason:` structure must be gone. Asserted STRUCTURALLY, not by searching
    # for the reason TEXT — unlike the consume/repark/unconsume strip sites,
    # this one RETIRES before stripping, and since Finding A the retirement
    # note deliberately folds each leg's authored reason into `blocking_notes`.
    # A bare text search would now fail on the correct behaviour.
    for orphan in ("  legs:", "    - kind:", "      reason:"):
        assert orphan not in fm, (
            f"no orphaned continuation line {orphan!r} may survive the strip; got {fm!r}"
        )
    assert "manual check pending" in (read_fm_field(fm, "blocking_notes") or ""), (
        "the leg's authored reason is retired into blocking_notes, not destroyed"
    )


def test_gate_cascade_clear_narrow_leaves_gate_evidence_untouched(handoff_repo):
    """C7 (AC10): the NARROW branch stays awaiting_gate and does NOT strip
    gate_evidence -- matches its gate_dependency-prose-reduction-only (not
    full-strip) treatment there."""
    _seed_blocker(handoff_repo, "blocker-narrow-b.md", "stub-nb", "shipped")
    _seed_blocker(handoff_repo, "blocker-narrow-c.md", "stub-nc", "in_flight")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-narrow-evidence.md", "open",
        deployment_state="awaiting_gate",
        extra=(
            _roadmap_extra(
                "gcc-narrow-evidence", "['stub-nb', 'stub-nc']", "stub-nb work, stub-nc work"
            )
            + "\ngate_evidence:\n"
            "  covers_prose: true\n"
            "  legs:\n"
            "    - leg_id: leg-human\n"
            "      kind: human\n"
            "      reason: manual check pending"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-narrow-evidence.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-nb"], ["b" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "awaiting_gate"
    assert "gate_evidence:" in fm, "narrow must leave gate_evidence untouched"
    assert "manual check pending" in fm


def test_gate_cascade_clear_partial_narrows_stays_awaiting_gate(handoff_repo):
    """One-of-two-shipped → NARROW: blocked_by shrinks, deployment_state stays
    awaiting_gate, gate_cleared_by stamped for the shipped one only, gate_dependency
    reduced but not fully stripped (the Staff Engineer F2 — narrow-and-stay keeps partial prose)."""
    _seed_blocker(handoff_repo, "blocker-b.md", "stub-b", "shipped")
    _seed_blocker(handoff_repo, "blocker-c.md", "stub-c", "in_flight")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-narrow.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra(
            "gcc-narrow", "['stub-b', 'stub-c']", "stub-b work, stub-c work"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-narrow.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-b"], ["b" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "awaiting_gate", (
        "partial clear must NEVER silently flip deployment_state"
    )
    blocked_by = read_fm_field(fm, "blocked_by") or ""
    assert "stub-b" not in blocked_by, "cleared blocker must be removed from blocked_by"
    assert "stub-c" in blocked_by, "unresolved blocker must remain in blocked_by"
    assert "b" * 40 in (read_fm_field(fm, "gate_cleared_by") or "")
    gate_dep = read_fm_field(fm, "gate_dependency")
    assert gate_dep is not None, (
        "narrow-and-stay must NOT fully strip gate_dependency (the Staff Engineer F2)"
    )
    assert "stub-b" not in gate_dep, "matched clause for the cleared blocker must be dropped"


def test_gate_cascade_clear_narrow_does_not_drop_sibling_prefix_family_clause(handoff_repo):
    """C8 clause-ownership fix: clearing a blocker whose id is a PREFIX of a
    sibling's id (pacl-05-a vs pacl-05-a-inject) must drop only the clause
    naming the cleared blocker and must NOT also drop the sibling's clause
    merely because the cleared id is a substring of it.

    Pre-fix, `bid not in c` matched "pacl-05-a" as a substring of
    "pacl-05-a-inject", so clearing pacl-05-a would have silently dropped the
    still-blocking pacl-05-a-inject's own clause too.
    """
    _seed_blocker(handoff_repo, "blocker-pacl-a.md", "pacl-05-a", "shipped")
    _seed_blocker(handoff_repo, "blocker-pacl-a-inject.md", "pacl-05-a-inject", "in_flight")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-prefix-family.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra(
            "gcc-prefix-family",
            "['pacl-05-a', 'pacl-05-a-inject']",
            "pacl-05-a work, pacl-05-a-inject work",
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-prefix-family.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["pacl-05-a"], ["a" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "awaiting_gate"
    blocked_by = read_fm_field(fm, "blocked_by") or ""
    assert "pacl-05-a-inject" in blocked_by
    gate_dep = read_fm_field(fm, "gate_dependency") or ""
    assert "pacl-05-a work" not in gate_dep, "cleared blocker's own clause must be dropped"
    assert "pacl-05-a-inject work" in gate_dep, (
        "sibling clause must SURVIVE -- substring match must not treat "
        "pacl-05-a as owning pacl-05-a-inject's clause"
    )


def test_gate_cascade_clear_fails_loud_on_full_drain_narrow(handoff_repo):
    """Slice-B review Finding 1 (P1): a single-clause gate_dependency prose that
    ONLY names the cleared blocker (stub-b) drains to empty on reduction while
    blocked_by still has a remaining member (stub-c) — the narrow branch would
    otherwise fire (new_blocked_by non-empty) but silently no-op the prose
    write, leaving stale text still naming the already-cleared stub-b. This
    must fail loud (no write) instead."""
    _seed_blocker(handoff_repo, "blocker-b2.md", "stub-b", "shipped")
    _seed_blocker(handoff_repo, "blocker-c2.md", "stub-c", "in_flight")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-drain.md", "open",
        deployment_state="awaiting_gate",
        # Single-clause prose naming ONLY stub-b — stub-c is tracked purely via
        # the structured blocked_by edge, with no matching prose clause.
        extra=_roadmap_extra("gcc-drain", "['stub-b', 'stub-c']", "stub-b work"),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-drain.md")
    fm_before = _read_fm(abs_path)

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-b"], ["b" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] != 0, f"expected fail-loud; result={result!r}"
    assert result["applied"] is False, result

    # No write occurred — frontmatter unchanged.
    fm_after = _read_fm(abs_path)
    assert fm_after == fm_before, "fail-loud path must not write anything"
    assert read_fm_field(fm_after, "deployment_state") == "awaiting_gate"
    assert "stub-b" in (read_fm_field(fm_after, "blocked_by") or "")
    assert "stub-b" in (read_fm_field(fm_after, "gate_dependency") or "")


def test_gate_cascade_clear_fails_loud_on_asymmetry(handoff_repo):
    """blocker_ids/blocker_shas length mismatch fails loud, no write."""
    _seed_blocker(handoff_repo, "blocker-d.md", "stub-d", "shipped")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-asym.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra("gcc-asym", "['stub-d']", "stub-d work"),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-asym.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-d"], []),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, f"asymmetric blocker_ids/blocker_shas must fail loud; got {result!r}"
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original, (
        "asymmetry must not write the file"
    )


def test_gate_cascade_clear_fails_loud_on_duplicate_blocker_id(handoff_repo):
    """Slice-B review Finding 4 (P2): two DISTINCT handoffs sharing the same
    stub_id (a stub_id-uniqueness invariant violation) must resolve as
    ambiguous, not silently trust glob-sort order and pick one — the act-time
    re-verification gate must fail loud, no write, even though one of the two
    duplicate-id handoffs genuinely IS shipped."""
    _seed_blocker(handoff_repo, "blocker-dup-1.md", "stub-dup", "shipped")
    _seed_blocker(handoff_repo, "blocker-dup-2.md", "stub-dup", "in_flight")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-dup.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra("gcc-dup", "['stub-dup']", "stub-dup work"),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-dup.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-dup"], ["d" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] != 0, (
        f"duplicate blocker id must fail loud, never pick a match by glob-sort "
        f"order; got {result!r}"
    )
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original, (
        "ambiguous duplicate-id claim must not write the file"
    )


def test_gate_cascade_clear_fails_loud_on_stale_shipped_claim(handoff_repo):
    """Act-time re-verification (the Staff Engineer F0): caller claims a blocker is shipped,
    but its LIVE deployment_state is 'continued' — gate-cascade-clear MUST fail
    loud and perform NO write, never trusting the caller-supplied claim."""
    _seed_blocker(handoff_repo, "blocker-e.md", "stub-e", "continued")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-stale.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra("gcc-stale", "['stub-e']", "stub-e work"),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-stale.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-e"], ["e" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"stale shipped claim (live state != shipped) must fail loud; got {result!r}"
    )
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original, (
        "stale-claim fail-loud must not write the gated handoff"
    )


def test_gate_cascade_clear_fails_loud_on_unresolvable_blocker_id(handoff_repo):
    """A blocker id that resolves to NO handoff at all also fails loud, no write —
    unresolvable is treated identically to not-currently-shipped."""
    handoff_repo.seed_handoff(
        "2026-01-01-gcc-unresolvable.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra("gcc-unresolvable", "['stub-ghost']", "stub-ghost work"),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-unresolvable.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-ghost"], ["f" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_resolve_blocker_deployment_state_month_nested_archive_resolves(handoff_repo):
    """Break-class regression: a shipped blocker archived to the month-nested
    archive/handoffs/YYYY-MM/<file> layout (per handoff_archive_dest) must
    still resolve — the non-recursive glob previously scanned archive/handoffs/
    flat only and returned None for every archived blocker, wedging
    _gate_cascade_clear's 'not currently shipped' fail-loud path forever."""
    archived_dir = handoff_repo.root / "archive" / "handoffs" / "2026-07"
    archived_dir.mkdir(parents=True)
    (archived_dir / "blocker-nested.md").write_text(
        '---\nstub_id: "stub-nested"\ndeployment_state: shipped\n---\n\n# Handoff\n',
        encoding="utf-8",
    )

    result = _mod._resolve_blocker_deployment_state("stub-nested", handoff_repo.root)

    assert result == "shipped"


def test_resolve_blocker_deployment_state_flat_archive_still_resolves(handoff_repo):
    """Guard against regressing the pre-existing flat archive/handoffs/<file>
    layout (no month subdir) — it must continue to resolve after the fix."""
    flat_dir = handoff_repo.root / "archive" / "handoffs"
    flat_dir.mkdir(parents=True)
    (flat_dir / "blocker-flat.md").write_text(
        '---\nstub_id: "stub-flat"\ndeployment_state: shipped\n---\n\n# Handoff\n',
        encoding="utf-8",
    )

    result = _mod._resolve_blocker_deployment_state("stub-flat", handoff_repo.root)

    assert result == "shipped"


def test_resolve_blocker_deployment_state_skips_live_root_dot_archive(handoff_repo):
    """state/handoffs/.archive/ is a stale local archive sibling to the live
    root — it must NOT be descended into. A live handoff and a stale
    .archive/ duplicate sharing the same stub_id must resolve to the LIVE
    handoff's deployment_state only, never trip the duplicate-id ambiguity
    guard (proving .archive/ was skipped, not merely deprioritized)."""
    _seed_blocker(handoff_repo, "blocker-live.md", "stub-live-vs-archive", "ready_to_fire")

    dot_archive_dir = handoff_repo.root / "state" / "handoffs" / ".archive"
    dot_archive_dir.mkdir(parents=True)
    (dot_archive_dir / "blocker-live-old.md").write_text(
        '---\nstub_id: "stub-live-vs-archive"\ndeployment_state: shipped\n---\n\n# Handoff\n',
        encoding="utf-8",
    )

    result = _mod._resolve_blocker_deployment_state("stub-live-vs-archive", handoff_repo.root)

    assert result == "ready_to_fire"
    assert result != _mod._AMBIGUOUS_BLOCKER_SENTINEL


def test_gate_cascade_clear_fails_loud_on_non_awaiting_gate_source(handoff_repo):
    """gate-cascade-clear requires deployment_state:awaiting_gate at mutation time."""
    _seed_blocker(handoff_repo, "blocker-f.md", "stub-f", "shipped")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-wrong-state.md", "claimed",
        deployment_state="in_flight",
        extra=_roadmap_extra("gcc-wrong-state", "['stub-f']"),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-wrong-state.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-f"], ["f" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_gate_cascade_clear_idempotent_at_full_target_state(handoff_repo):
    """No-op when blocked_by is already empty AND deployment_state is already
    ready_to_fire (an empty-removal-set replay of a completed clear)."""
    handoff_repo.seed_handoff(
        "2026-01-01-gcc-idempotent.md", "open",
        deployment_state="ready_to_fire",
        extra=(
            _roadmap_extra("gcc-idempotent", "[]")
            + '\ngate_cleared_by: ["' + ("a" * 40) + '"]'
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-idempotent.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-a"], ["a" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_gate_cascade_clear_fails_loud_when_blocker_id_not_in_blocked_by(handoff_repo):
    """Requesting removal of a blocker id absent from blocked_by fails loud, no write —
    guards against a mis-scoped or stale removal request."""
    _seed_blocker(handoff_repo, "blocker-g.md", "stub-g", "shipped")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-not-present.md", "open",
        deployment_state="awaiting_gate",
        extra=_roadmap_extra("gcc-not-present", "['stub-h']", "stub-h work"),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-not-present.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-g"], ["g" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_gate_cascade_clear_flip_never_speaks_a_stale_prior_recheck_verdict(handoff_repo):
    """A cascade-clear FLIP must never present an EARLIER gate-recheck's verdict
    as its own finding (Review: code-reviewer — Finding B).

    `gate_evidence` and `blocked_by` are independent fields, so a prior BARE
    (`cleared=False`) gate-recheck can leave a `gate_evidence_results:` block
    reading `still-blocked` with a months-old `checked_at` while the record
    later flips to ready_to_fire on an unrelated blocker-SHA clear. Against the
    pre-fix renderer this record retires as
    `gate_evidence retired (still-blocked, checked_at 2026-01-01): …` — a
    stale, self-contradicting verdict stamped onto a record that just became
    ready_to_fire. The prior finding is preserved, but only as a dated `prior`
    observation.
    """
    _seed_blocker(handoff_repo, "blocker-stale-results.md", "stub-stale", "shipped")

    handoff_repo.seed_handoff(
        "2026-01-01-gcc-flip-stale-results.md", "open",
        deployment_state="awaiting_gate",
        extra=(
            _roadmap_extra("gcc-flip-stale", "['stub-stale']", "stub-stale work must ship")
            + "\ngate_evidence:\n"
            "  covers_prose: true\n"
            "  legs:\n"
            "    - leg_id: leg-human\n"
            "      kind: human\n"
            "      reason: manual check pending\n"
            "gate_evidence_results:\n"
            "  status: still-blocked\n"
            '  checked_at: "2026-01-01"\n'
            "  legs:\n"
            "    - leg_id: leg-human\n"
            "      kind: human\n"
            "      status: indeterminate\n"
            "      reason: human leg never resolves"
        ),
    )
    abs_path = handoff_repo.abs_path("2026-01-01-gcc-flip-stale-results.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-stale"], ["5" * 40]),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    notes = read_fm_field(fm, "blocking_notes") or ""

    assert "gate_evidence retired (still-blocked" not in notes, (
        "the stale prior verdict must never head the note as this clear's own "
        f"finding; got {notes!r}"
    )
    assert "not re-verified by this clear" in notes, (
        f"the note must say the legs were not re-verified here; got {notes!r}"
    )
    assert "prior gate-recheck still-blocked at 2026-01-01" in notes, (
        f"the prior finding is preserved, dated and labelled prior; got {notes!r}"
    )
    assert "leg-human [human] not-rechecked" in notes, (
        f"each leg reports not-rechecked by THIS clear; got {notes!r}"
    )
    assert "prior: indeterminate" in notes, (
        f"the prior per-leg verdict is not discarded either; got {notes!r}"
    )


def test_not_rechecked_retirement_keeps_each_legs_authored_reason_and_note():
    """The not-rechecked fallback must carry each leg's own authored `reason`
    and `note`, exactly as the results branch carries `reason` (Review:
    code-reviewer — Finding A).

    This is the ONE path where the block is destroyed with no prior recheck, so
    the author's own words are the entire paper trail. The pre-fix fallback
    rendered `leg_id [kind] not-rechecked` and nothing else, discarding a
    `human` leg's "manual check pending" and a `file-exists` leg's
    schema-REQUIRED `note` (handoff.schema.json § gate_evidence.legs.note)
    while both sat on disk.
    """
    note = _mod._render_gate_evidence_retirement(
        {
            "covers_prose": True,
            "legs": [
                {
                    "leg_id": "leg-human",
                    "kind": "human",
                    "reason": "manual check pending",
                },
                {
                    "leg_id": "leg-file",
                    "kind": "file-exists",
                    "ref": "marker.txt",
                    "expected": True,
                    "note": "marker file presence proves the migration ran",
                },
            ],
        },
        None,
        rechecked_by_this_clear=False,
    )

    assert "leg-human [human] not-rechecked — manual check pending" in note, (
        f"an authored reason must survive the fallback; got {note!r}"
    )
    assert "(note: marker file presence proves the migration ran)" in note, (
        f"an authored note must survive the fallback; got {note!r}"
    )


def test_malformed_results_block_is_named_not_silently_ignored():
    """A valid `gate_evidence:` alongside a present-but-MALFORMED
    `gate_evidence_results:` must say so (Review: code-reviewer — Finding C).

    The standalone-malformed-`gate_evidence` case already reports "malformed";
    the pre-fix code took the not-rechecked branch here with no mention at all,
    so a reader could not tell that a results block existed on disk and was
    destroyed unread.
    """
    note = _mod._render_gate_evidence_retirement(
        {"covers_prose": True, "legs": [{"leg_id": "leg-a", "kind": "human"}]},
        "not a mapping at all",
        rechecked_by_this_clear=False,
    )

    assert "malformed" in note, (
        f"an unreadable results block must be named, not silently dropped; got {note!r}"
    )
    assert "leg-a" in note, f"the readable legs are still rendered; got {note!r}"


# ---------------------------------------------------------------------------
# Frontmatter ARRAY-field helpers — present-but-empty key, LF and CRLF
#
# Review: code-reviewer — Finding D. These helpers carried a hand-copied
# duplicate of the pre-fix `key:(?=[ \t]|$)\s*` pattern that
# coordinator_core.frontmatter.primitives was root-fixed for; they now route
# key resolution through `primitives._fm_key_line_pattern`. A bare
# `blocked_by:` / `gate_cleared_by:` left by a prior clear is a live on-disk
# shape, so both defect halves were reachable in production.
# ---------------------------------------------------------------------------


def test_replace_fm_array_field_on_empty_key_does_not_eat_the_next_line_lf():
    """Pre-fix, `\\s*` in the captured prefix crossed the newline after a bare
    `blocked_by:` and the trailing `.*$` then matched and REPLACED the
    following, unrelated line."""
    fm = "blocked_by:\nkind: spinoff-roadmap\nstatus: open\n"

    out = _mod._replace_fm_array_field(fm, "blocked_by", ["stub-a"])

    assert out == "blocked_by: ['stub-a']\nkind: spinoff-roadmap\nstatus: open\n", (
        f"the neighbouring field must survive verbatim; got {out!r}"
    )


def test_replace_fm_array_field_on_empty_key_crlf():
    """Pre-fix, the `(?=[ \\t]|$)` lookahead rejected `\\r`, so a CRLF
    present-but-empty key read as ABSENT and the replace silently no-opped."""
    fm = "blocked_by:\r\nkind: spinoff-roadmap\r\nstatus: open\r\n"

    out = _mod._replace_fm_array_field(fm, "blocked_by", ["stub-a"])

    assert out == "blocked_by: ['stub-a']\r\nkind: spinoff-roadmap\r\nstatus: open\r\n", (
        f"a CRLF empty key must be filled, keeping its own line ending; got {out!r}"
    )
    assert "\n" not in out.replace("\r\n", ""), f"no mixed line endings; got {out!r}"


def test_insert_fm_array_field_anchors_on_an_empty_crlf_key():
    """Pre-fix, an empty CRLF `blocked_by:` anchor did not match, silently
    degrading the anchored insert into an append-at-end that also wrote a bare
    LF into a CRLF document."""
    fm = "blocked_by:\r\nkind: spinoff-roadmap\r\nstatus: open\r\n"

    out = _mod._insert_fm_array_field(fm, "gate_cleared_by", ["a" * 40], "blocked_by")

    lines = out.split("\r\n")
    assert lines[0] == "blocked_by:", f"anchor line untouched; got {out!r}"
    assert lines[1] == f"gate_cleared_by: ['{'a' * 40}']", (
        f"the new line must land directly after its anchor; got {out!r}"
    )
    assert "\n" not in out.replace("\r\n", ""), f"no mixed line endings; got {out!r}"


def test_insert_fm_array_field_anchors_on_an_empty_lf_key():
    """The LF half of the same anchor case — an empty `blocked_by:` anchor is
    matched and the insert lands immediately after it, not at end-of-document."""
    fm = "blocked_by:\nkind: spinoff-roadmap\nstatus: open\n"

    out = _mod._insert_fm_array_field(fm, "gate_cleared_by", ["a" * 40], "blocked_by")

    assert out == (
        "blocked_by:\n"
        f"gate_cleared_by: ['{'a' * 40}']\n"
        "kind: spinoff-roadmap\n"
        "status: open\n"
    ), f"anchored insert directly after the empty anchor; got {out!r}"


def test_gate_cascade_clear_rejects_traversal_path(handoff_repo):
    """gate-cascade-clear also rejects traversal paths — same containment guard."""
    secret = handoff_repo.root / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        _gate_cascade_clear_params(
            "state/handoffs/../../secret.md", ["stub-x"], ["x" * 40]
        ),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# reaped_from_session (C3a) — the transition-seam coverage of C2's opt-in
# reaper provenance signal on the unclaim verb.
#
# Spec backlink: docs/plans/2026-08-05-reaper-preserves-closure-evidence.md
# § C2 (_unclaim's reaped_from param) and § C3a (this file).
#
# AC3(b): a direct cs_unclaim_handoff(..., note, reaped_from=<sid>)-shaped
# call (here: dispatching the unclaim verb through _handler with
# params["reaped_from"] set — the cheaper stand-in for the bin reaper's
# release path named in the chunk body) leaves the handoff carrying
# reaped_from_session == the passed sid.
#
# AC6 sid-resolution order — three cases: (i) frontmatter claimed_by wins
# over any caller-supplied fallback; (ii) claimed_by absent, consumed_by
# present -> used; (iii) neither present and no fallback supplied -> unclaim
# still succeeds (exit_code 0), no reaped_from_session written, stderr names
# the handoff and the skip.
#
# Pinned no-op-arm interaction: a handoff already at status:open +
# deployment_state:ready_to_fire makes _unclaim's idempotency guard fire
# BEFORE the reaped_from_session resolution block runs (old_text returned
# byte-identical) -- opting in with reaped_from set must NOT write the field
# on that no-op arm. Nothing was stripped on this arm, so there is nothing
# to preserve; this is the exact silence that let C5's backfill and C2's
# live path disagree (see chunk body).
# ---------------------------------------------------------------------------


def _unclaim_params(
    handoff_path: str, note: str = "", reaped_from: Optional[str] = None
) -> dict:
    params: dict = {"verb": "unclaim", "handoff_path": handoff_path}
    if note:
        params["note"] = note
    if reaped_from is not None:
        params["reaped_from"] = reaped_from
    return params


#: UUID-shaped test doubles (obviously-synthetic repeated-hex-digit form) —
#: _unclaim's reaped_from_session resolution gate (_is_session_id_shaped)
#: requires a 36-char UUID shape, so these stand-ins must satisfy it too.
_REAPED_FROM_SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_unclaim_reaped_from_writes_reaped_from_session(handoff_repo):
    """AC3(b): opting in with reaped_from=<sid> on a live in_flight handoff
    (claimed_by present, distinct from the passed sid) leaves
    reaped_from_session == the FRONTMATTER claimed_by (AC6(i) resolution
    order), not the passed sid — proven by using a DIFFERENT sid as the
    caller-supplied fallback so the two cannot be conflated."""
    holder_sid = "11111111-1111-1111-1111-111111111111"
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-happy.md", "claimed",
        deployment_state="in_flight",
        claimed_at=_AT,
        claimed_by=holder_sid,
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-happy.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from=_REAPED_FROM_SID),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "status") == "open"
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert read_fm_field(fm, "reaped_from_session") == holder_sid


def test_unclaim_reaped_from_sid_resolution_claimed_by_wins(handoff_repo):
    """AC6(i): frontmatter claimed_by wins over the caller-supplied
    reaped_from fallback, even when both are present and differ."""
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-claimed-by-wins.md", "claimed",
        deployment_state="ready_to_fire",
        claimed_by="22222222-2222-2222-2222-222222222222",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-claimed-by-wins.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from=_REAPED_FROM_SID),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") == "22222222-2222-2222-2222-222222222222"


def test_unclaim_reaped_from_sid_resolution_falls_back_to_consumed_by(handoff_repo):
    """AC6(ii): claimed_by absent, consumed_by present (legacy pre-DR-084
    field name) -> consumed_by is used, still winning over the
    caller-supplied reaped_from fallback."""
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-consumed-by.md", "claimed",
        deployment_state="in_flight",
        extra="consumed_by: 33333333-3333-3333-3333-333333333333",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-consumed-by.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from=_REAPED_FROM_SID),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") == "33333333-3333-3333-3333-333333333333"


def test_unclaim_reaped_from_sid_resolution_falls_back_to_caller_supplied(handoff_repo):
    """AC6 fallback leg: neither claimed_by nor consumed_by present in
    frontmatter -> the caller-supplied reaped_from value itself is written
    (a legacy pid-only claim shape whose frontmatter never carried a
    resolvable sid)."""
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-caller-fallback.md", "claimed",
        deployment_state="in_flight",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-caller-fallback.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from=_REAPED_FROM_SID),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") == _REAPED_FROM_SID


def test_unclaim_reaped_from_no_resolvable_sid_skips_write_and_logs(handoff_repo, capsys):
    """AC6(iii): neither claimed_by/consumed_by present AND no
    reaped_from fallback supplied by the caller (empty string -> None per
    _handler's own str.strip()-or-None normalization) -> _unclaim still
    succeeds (exit_code 0), no reaped_from_session key is written, and
    stderr names the handoff and the skip.

    Uses "" (not omitting the param) so this exercises _handler's own
    params.get("reaped_from") normalization path, not merely _unclaim's
    reaped_from=None default — reaped_from is not None is the gate that
    fires the resolution block at all, and _handler is what turns an empty
    caller string into that None.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-no-sid.md", "claimed",
        deployment_state="in_flight",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-no-sid.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from=""),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["applied"] is True

    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") is None, (
        "no resolvable sid on any of the three legs -> no key written"
    )

    captured = capsys.readouterr()
    assert abs_path in captured.err or "2026-01-01-reaped-no-sid.md" in captured.err, (
        f"stderr must name the handoff; got {captured.err!r}"
    )
    assert "reaped_from_session not written" in captured.err, (
        f"stderr must name the skip; got {captured.err!r}"
    )


def test_unclaim_reaped_from_rejects_unknown_shape(handoff_repo, capsys):
    """_is_session_id_shaped's allowlist rejects the literal "unknown" —
    it is not UUID-shaped, so no candidate resolves and no
    reaped_from_session key is written (mirrors the no-resolvable-sid arm)."""
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-unknown-shape.md", "claimed",
        deployment_state="in_flight",
        claimed_by="unknown",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-unknown-shape.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from="unknown"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") is None
    captured = capsys.readouterr()
    assert "reaped_from_session not written" in captured.err


def test_unclaim_reaped_from_rejects_bare_pid_shape(handoff_repo, capsys):
    """A bare-PID string (all-digits, the other known-bad shape a prior
    provenance source could emit) is also rejected by the allowlist — no
    reaped_from_session written."""
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-pid-shape.md", "claimed",
        deployment_state="in_flight",
        claimed_by="12345",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-pid-shape.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from="12345"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") is None
    captured = capsys.readouterr()
    assert "reaped_from_session not written" in captured.err


def test_unclaim_reaped_from_skips_bogus_candidate_accepts_later_valid(handoff_repo):
    """A non-matching earlier candidate (frontmatter claimed_by, not
    session-id-shaped) is skipped and resolution continues to the next
    candidate in the chain — here, the caller-supplied reaped_from fallback,
    which IS session-id-shaped and wins."""
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-skip-to-fallback.md", "claimed",
        deployment_state="in_flight",
        claimed_by="unknown",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-skip-to-fallback.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from=_REAPED_FROM_SID),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") == _REAPED_FROM_SID


def test_unclaim_reaped_from_all_candidates_bogus_unclaim_still_succeeds(handoff_repo, capsys):
    """All three candidates non-session-id-shaped -> nothing written, but
    the unclaim itself still succeeds (release is never blocked on missing
    provenance) and stderr names the skip."""
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-all-bogus.md", "claimed",
        deployment_state="in_flight",
        claimed_by="unknown",
        extra="consumed_by: 999",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-all-bogus.md")

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from="unknown"),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is True
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") is None
    captured = capsys.readouterr()
    assert "reaped_from_session not written" in captured.err
    assert abs_path in captured.err or "2026-01-01-reaped-all-bogus.md" in captured.err


def test_unclaim_reaped_from_no_op_arm_writes_nothing(handoff_repo):
    """Pinned interaction: a handoff ALREADY at status:open +
    deployment_state:ready_to_fire hits _unclaim's idempotency no-op guard
    BEFORE the reaped_from_session resolution block ever runs. Opting in
    with reaped_from set on this arm must not write reaped_from_session —
    the file stays byte-identical (applied=False), same as the pre-existing
    no-op assertion for the vanilla verb, exercised here with reaped_from
    populated to prove opting in changes nothing on this arm.
    """
    handoff_repo.seed_handoff(
        "2026-01-01-reaped-no-op.md", "open",
        deployment_state="ready_to_fire",
    )
    abs_path = handoff_repo.abs_path("2026-01-01-reaped-no-op.md")
    original_content = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _unclaim_params(abs_path, reaped_from=_REAPED_FROM_SID),
        repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["applied"] is False, "already-at-target must stay a no-op even opted in"

    assert open(abs_path, encoding="utf-8").read() == original_content, (
        "idempotent no-op must not modify the file, including no "
        "reaped_from_session write"
    )
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "reaped_from_session") is None
