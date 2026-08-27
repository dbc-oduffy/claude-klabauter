"""
coordinator_core.ops.handoff_reconcile — `handoff.reconcile_open` (REBUILT, C4,
pln-reconcile-open-comes-back-under-the-bar).

Rebuilt from FIRST PRINCIPLES against C3's requirements audit
(`state/audits/2026-08-25-where-reconcile-open-spends-its-cpu.md`) after DR-344's kill-bar
deleted the prior 2,721-line implementation (C2b) — no line of the deleted module was read to
write this one (only its git history, per the plan's `## Anti-scope` authorship carve-out, to
check completeness).

**REDUCED BUILD TARGET (2026-08-26).** C10 killed `commit_reality.py`'s shipped-ness verdict
(`evaluate_commit_reality`, DEC-1/C2) outright — the delete is permanent
(`state/kill-ledger.md`), not a deferred half of this op. This module therefore computes ONLY
the gate-evaluation half of the original design: the four-value verdict enum `clear` / `narrow` /
`surface` / `not-cleared` (C3, `coordinator_core.reconcile.gate_eval.evaluate_gate`). `auto-ship`,
the `reconciled[]` array, and any commit-reality/DEC-1 routing are GONE, not reduced — there is no
shipping-verdict half left to call.

Return shape:
    {
      "gates_cleared": [ {handoff_id, handoff_path, action: "gate-cascade-clear",
                           verdict: "clear"|"narrow", blocker_ids: [...], dry_run: bool,
                           applied: bool, exit_code?: int, message?: str} ],
      "surfaced": [ {handoff_id, reason, evidence: [...]} ],
      "exit_code": 0 | 1,
    }

Requirements this module discharges (full derivation:
`state/audits/2026-08-25-where-reconcile-open-spends-its-cpu.md` §1, rows R1-R16 minus the
shipped-ness rows R9/R10 struck by C10's kill — commit-reality's mechanical-commit-denylist and
cross-handoff-attribution guards no longer exist to bind):

  - R1 (narrowed): routing carries the surviving FOUR verdict values — `clear`/`narrow`/`surface`/
    `not-cleared` — never collapsed to two-way. `auto-ship` is gone with C10.
  - R2: `not-cleared` is load-bearing SILENCE — no entry in `gates_cleared[]`/`surfaced[]`, no
    transition, no history-map entry. See the `not-cleared` branch below.
  - R3: `narrow`+surface composite — a `narrow` verdict whose `also_surface` is True (gate_eval's
    own computation) gets BOTH a `gates_cleared[]` row (the narrow mutation) AND a `surfaced[]`
    row (the dead-blocker warning), same handoff.
  - R4 (DR-266 § 93): the verdict ladder's implicit fall-through is REPRODUCED, not closed — an
    unrecognized `verdict` value (there is no fifth today; this is a defensive mirror of the
    deleted op's own un-exhaustive if/elif chain) silently falls through with no array entry and
    no transition, exactly like the deleted ladder. Closing it would be a behaviour change
    requiring its own decision record (DR-266 § 93) — not made here. See the fall-through comment
    at the bottom of the per-handoff loop.
  - R5: WRITER in every mode, dry-run included — `_save_surfaced_history` runs unconditionally,
    every call, regardless of `dry_run` or whether anything surfaced (an empty map still writes).
    DR-300 blocks `/pickup` from calling this op for exactly this reason; this rebuild offers no
    read-only mode (that would need DR-300 revisited, not assumed).
  - R6 / R6b (DR-299 — DO NOT ARM): `dry_run` resolution is POLICY-AUTHORITATIVE
    (`_resolve_dry_run`) — the loaded policy's `dry_run` key (fail-closed default `True` on any
    absent/malformed policy, see `policy_loader._conservative_policy`) wins over a caller-supplied
    `params["dry_run"]` unless the caller also supplies a non-empty `dry_run_override_reason`,
    logged at WARNING either way. This governs `gates_cleared[]`'s `applied` field and whether
    `_gate_cascade_clear` is actually invoked — never a default flip.
  - R7: `policy_path` is a test/CLI injection seam only (forwarded to
    `policy_loader.load_policy`); production callers omit it.
  - R8: `exit_code` is always `0` except the `repo_root is None` guard (no socket-authoritative
    common_dir), which returns `exit_code: 1` with both arrays empty.
  - R11: prose `gate_dependency` gates never auto-transition. Enforced by construction: this
    module never assembles `witness_candidates` for `evaluate_gate`'s PROSE fallback path (always
    passes `None`) — a prose gate resolves at most `surface` (zero witnesses is the conservative
    branch), so it can never reach `gates_cleared[]`'s clear/narrow routing, which only ever fires
    on the STRUCTURED path's verdict. Wiring live witness-candidate collection is out of this
    chunk's requirements table and is not built here.
  - R12 (DR-320 prose-vs-evidence guard, keyed on `gate_evidence_resolved`): `gate_evidence` is
    read + live-resolved per awaiting_gate handoff via the shared leaf
    `coordinator_core.ops.handoff_transition._read_gate_evidence_resolved` (the same reader
    `handoff_gate_aging.classify_gate` uses — one shared resolver, never re-derived).
    `gate_eval.consumes_gate_evidence(handoff, gate_evidence)` is the single source of truth for
    whether `evaluate_gate` actually reached rule 0 for this handoff. Evidence present but NOT
    consumed (`gate_evidence_resolved is False`) is intercepted onto the `surfaced[]`-only path
    with a reason naming the distinction, REGARDLESS of what `verdict` says — never re-derived as
    an equivalent-looking presence check.
  - R13: no cadence wiring here — this op remains `workday-start`-gated by convention, not called
    from `session.boot_sweep` (nothing in this module enforces that; it is a caller-side
    recommendation per the producer contract § 5).
  - R14: consumer contract (DoE's `workday-start.md` § 1.10.6) is unaffected by this rebuild's
    internals — same op name, same two live output arrays' shapes minus `reconciled[]`.

Deliberately NOT built here (named, not silently dropped):
  - Auto-ship / commit-reality routing — killed by C10, no replacement.
  - D1 severed-observer conservation assertion (previous-run vs. this-run surfaced-id diffing) —
    not named as a surviving requirement in C3's audit; the write this rebuild performs
    (`_save_surfaced_history`) is R5's WRITER obligation only, not a promise to also re-implement
    the conservation check that used to consume it.
  - Live witness-candidate collection for the PROSE fallback path — see R11 above.

Negative-spec:
  - Does NOT run git, and does NOT call `commit_reality.py` (killed by C10) — this rebuild's
    entire reachable set is COMPUTE_ONLY (`gate_eval`) plus a plain frontmatter/directory walk
    (`handoff_corpus`) plus one JSON file write (`_save_surfaced_history`). Zero subprocess
    spawns on this op's own reachable set (`coordinator_core/tests/
    test_no_uncounted_spawn_on_budgeted_path.py`'s `_BUDGETED_ENTRYPOINTS` "measured empty" row).
  - Does NOT invent a read-only mode — dry_run controls only whether `_gate_cascade_clear` is
    invoked and `applied` is stamped, never whether `surfaced-history.json` is written (R5).
  - Does NOT re-derive `gate_evidence_resolved` from `gate_evidence` presence — always calls
    `gate_eval.consumes_gate_evidence`.
  - Does NOT batch-write multiple `state/handoffs/*.md` files itself — each
    `handoff.transition gate-cascade-clear` call it makes is its own independent, already
    DR-212-compliant single-file mutation (mirrors the killed op's own DR-212 compliance
    argument, producer contract § 2 — unaffected by this rebuild).

Spec backlinks: docs/plans/2026-08-25-reconcile-open-comes-back-under-the-bar.md § C4,
state/audits/2026-08-25-where-reconcile-open-spends-its-cpu.md,
docs/reference/reconcile-open-behavioural-contract.md,
coordinator_core/contract/handoff-reconcile-producer-contract.md.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.handoff_transition import (
    _gate_cascade_clear,
    _read_gate_evidence_resolved,
)
from coordinator_core.reconcile.gate_eval import consumes_gate_evidence, evaluate_gate
from coordinator_core.reconcile.handoff_corpus import (
    _collect_all_handoffs_for_gate_index,
    _collect_open_handoffs,
)
from coordinator_core.reconcile.policy_loader import load_policy

_LOG = logging.getLogger(__name__)

_AWAITING_GATE_STATE = "awaiting_gate"

#: R6b (DR-299) — the caller-param name that, when a non-empty string, is required to APPLY a
#: `dry_run` override against the loaded policy's own value. See `_resolve_dry_run`.
_DRY_RUN_OVERRIDE_REASON_PARAM = "dry_run_override_reason"

#: R5 — ephemeral run-history location, under the git COMMON dir (never the worktree — this is
#: session bookkeeping, not doctrine content, not git-tracked). Same location family as the
#: deleted op's own D1 mechanism (verified against DR-300's own citation of this exact path).
_RECONCILE_HISTORY_RELPATH = ("coordinator-sessions", "reconcile-history", "surfaced-history.json")


def _history_path(common_dir: Path) -> Path:
    """R5 — absolute path to the surfaced-history write target."""
    path = common_dir
    for segment in _RECONCILE_HISTORY_RELPATH:
        path = path / segment
    return path


def _save_surfaced_history(history_path: Path, surfaced_map: Dict[str, str]) -> None:
    """R5 — persist THIS run's surfaced-id map. Best-effort: a write failure here must not fail
    the run whose verdicts have already been computed and are about to be returned.
    """
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps({"surfaced": surfaced_map}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning(
            "handoff.reconcile_open: could not persist surfaced-history to %s: %s",
            history_path,
            exc,
        )


def _resolve_dry_run(
    policy: Dict[str, Any], params: Dict[str, Any]
) -> "tuple[bool, Optional[Dict[str, Any]]]":
    """R6/R6b (DR-299) — the loaded policy is the SOLE source of truth for `dry_run`.

    Returns `(effective_dry_run, override_info)`. `override_info` is `None` in the normal
    (no-override) case; a dict describing what happened whenever a caller-supplied
    `params["dry_run"]` disagreed with the policy value (applied or refused).

    `policy.get("dry_run", True)` already carries the fail-closed guarantee —
    `policy_loader._conservative_policy()` hard-codes `dry_run: True` on both the absent AND
    malformed branches — so an absent/malformed policy yields `dry_run=True` with zero caller
    involvement. Arming is only ever an explicit, present, valid, DoE-authored `dry_run: false`
    in the policy file.
    """
    policy_dry_run = policy.get("dry_run", True)
    if not isinstance(policy_dry_run, bool):
        policy_dry_run = True

    caller_dry_run = params.get("dry_run")
    if not isinstance(caller_dry_run, bool) or caller_dry_run == policy_dry_run:
        return policy_dry_run, None

    reason = params.get(_DRY_RUN_OVERRIDE_REASON_PARAM)
    if isinstance(reason, str) and reason.strip():
        _LOG.warning(
            "handoff.reconcile_open: dry_run OVERRIDE applied — policy declares dry_run=%s, "
            "caller requested dry_run=%s with reason %r",
            policy_dry_run,
            caller_dry_run,
            reason,
        )
        return caller_dry_run, {
            "applied": True,
            "policy_dry_run": policy_dry_run,
            "requested_dry_run": caller_dry_run,
            "reason": reason,
        }

    _LOG.warning(
        "handoff.reconcile_open: dry_run override REFUSED — caller requested dry_run=%s "
        "against policy's dry_run=%s with no non-empty %r reason; deferring to policy",
        caller_dry_run,
        policy_dry_run,
        _DRY_RUN_OVERRIDE_REASON_PARAM,
    )
    return policy_dry_run, {
        "applied": False,
        "policy_dry_run": policy_dry_run,
        "requested_dry_run": caller_dry_run,
        "reason": None,
    }


def _handoff_identifier(handoff: Dict[str, Any]) -> str:
    """Best available durable identifier for a handoff dict, in priority order."""
    for key in ("handoff_id", "id", "stub_id"):
        value = handoff.get(key)
        if isinstance(value, str) and value:
            return value
    path = handoff.get("_path")
    return path if isinstance(path, str) and path else "<unknown>"


async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `handoff.reconcile_open` handler — see module docstring for the full contract."""
    if repo_root is None:
        # R8 — no socket-authoritative common_dir. Both arrays empty, exit_code 1.
        return {"gates_cleared": [], "surfaced": [], "exit_code": 1}

    worktree = main_worktree_root(repo_root)

    policy_path_param = params.get("policy_path") if isinstance(params, dict) else None
    policy_result = load_policy(policy_path_param if isinstance(policy_path_param, str) else None)
    policy = policy_result.policy

    dry_run, dry_run_override = _resolve_dry_run(policy, params if isinstance(params, dict) else {})

    open_handoffs = _collect_open_handoffs(worktree)
    all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(worktree)
    scan_incomplete = bool(scan_errors)

    gates_cleared: List[Dict[str, Any]] = []
    surfaced: List[Dict[str, Any]] = []
    surfaced_map: Dict[str, str] = {}
    today = datetime.date.today()

    for handoff in open_handoffs:
        if handoff.get("deployment_state") != _AWAITING_GATE_STATE:
            continue

        handoff_id = _handoff_identifier(handoff)
        handoff_path = handoff.get("_path")

        gate_evidence: Optional[Dict[str, Any]] = None
        if isinstance(handoff_path, str) and handoff_path:
            gate_evidence = _read_gate_evidence_resolved(Path(handoff_path), today)

        result = evaluate_gate(
            handoff,
            all_handoffs,
            witness_candidates=None,  # R11 — see module docstring; prose path never clears here.
            scan_incomplete=scan_incomplete,
            scan_errors=scan_errors,
            gate_evidence=gate_evidence,
        )
        verdict = result.get("verdict")

        # R12 (DR-320) — evidence present but never consumed by rule 0: intercept onto
        # surfaced[]-only, regardless of what `verdict` says.
        gate_evidence_resolved = consumes_gate_evidence(handoff, gate_evidence)
        if gate_evidence is not None and not gate_evidence_resolved:
            surfaced.append(
                {
                    "handoff_id": handoff_id,
                    "reason": (
                        "gate_evidence present on disk but not consumed by evaluate_gate's "
                        "rule 0 (gate_evidence_resolved=False) — forced to surfaced[]-only "
                        "per DR-320's re-keyed prose-vs-evidence guard"
                    ),
                    "evidence": result.get("evidence", []),
                }
            )
            if isinstance(handoff_path, str) and handoff_path:
                surfaced_map[handoff_id] = handoff_path
            continue

        if verdict in ("clear", "narrow"):
            blocker_ids = result.get("cleared_blocker_ids") or []
            blocker_shas = result.get("cleared_by_shas") or []
            entry: Dict[str, Any] = {
                "handoff_id": handoff_id,
                "handoff_path": handoff_path,
                "action": "gate-cascade-clear",
                "verdict": verdict,
                "blocker_ids": blocker_ids,
                "dry_run": dry_run,
                "applied": False,
            }
            if not dry_run and isinstance(handoff_path, str) and handoff_path and blocker_ids:
                try:
                    live_result = _gate_cascade_clear(
                        handoff_path, list(blocker_ids), list(blocker_shas), worktree, repo_root
                    )
                except Exception as exc:  # noqa: BLE001 — a per-handoff mutation failure must
                    # never take the whole sweep down (R8: op-level exit_code stays 0; the
                    # failure is captured inside this handoff's own entry).
                    entry["exit_code"] = 1
                    entry["message"] = f"gate-cascade-clear raised: {exc}"
                else:
                    entry["applied"] = bool(live_result.get("applied"))
                    if "exit_code" in live_result:
                        entry["exit_code"] = live_result["exit_code"]
                    if live_result.get("message"):
                        entry["message"] = live_result["message"]
                    if live_result.get("error"):
                        entry["error"] = live_result["error"]
            gates_cleared.append(entry)

            if verdict == "narrow" and result.get("also_surface"):
                # R3 — narrow+surface composite.
                surfaced.append(
                    {
                        "handoff_id": handoff_id,
                        "reason": (
                            "narrow verdict's remaining_blockers includes a dead "
                            "(abandoned/continued/closed) blocker — narrow+surface composite"
                        ),
                        "evidence": result.get("evidence", []),
                    }
                )
                if isinstance(handoff_path, str) and handoff_path:
                    surfaced_map[handoff_id] = handoff_path
            continue

        if verdict == "surface":
            surfaced.append(
                {
                    "handoff_id": handoff_id,
                    "reason": "gate_eval verdict=surface",
                    "evidence": result.get("evidence", []),
                }
            )
            if isinstance(handoff_path, str) and handoff_path:
                surfaced_map[handoff_id] = handoff_path
            continue

        if verdict == "not-cleared":
            # R2 — load-bearing silence: no array entry, no transition, no history-map entry.
            continue

        # R4 (DR-266 § 93) — the verdict ladder's implicit fall-through, REPRODUCED not closed.
        # No verdict outside {clear, narrow, surface, not-cleared} exists today; this branch is
        # a defensive mirror of the deleted op's own un-exhaustive if/elif chain, kept
        # deliberately silent (no array entry, no raise) rather than closed into a raise or a
        # surfaced[] catch-all — closing it is a behaviour change needing its own decision
        # record, not made in this chunk.
        continue

    history_path = _history_path(repo_root)
    _save_surfaced_history(history_path, surfaced_map)  # R5 — writer in every mode, unconditional.

    response: Dict[str, Any] = {
        "gates_cleared": gates_cleared,
        "surfaced": surfaced,
        "exit_code": 0,
    }
    if dry_run_override is not None:
        response["dry_run_override"] = dry_run_override
    return response


register_op("handoff.reconcile_open", _handler)
