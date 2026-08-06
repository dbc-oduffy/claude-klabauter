"""
coordinator_core.ops.handoff_phase_stamp — JSON-RPC "handoff.stamp_phase" operation.

Purpose: claude-klabauter's generation-stamp mechanism for the example-doctrine-repo-landed ``handoff_phase``
schema contract (example-doctrine-repo ``coordinator/schemas/handoff.schema.json``, plan
``docs/plans/2026-07-17-execution-handoff-phase-doe-contract.md``). Stamps
``handoff_phase: {continuation|execution}`` onto a target ``state/handoffs/*.md``
baton and, when ``phase=execution``, the four-field authorization stamp
(``execution_authorized_{by,at,sha,note}``) — sourced from the cited PLAN's own
frontmatter (``plan_path``, the SOLE v1 value-source; see Decision D1,
``docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md``). Per the
PM's ruling ("claude-klabauter owns the marking, the skill just carries the invocation" —
DR-210 § Decision 1), this op is claude-klabauter's authoritative write path for the
execution-baton overlay; the example-doctrine-repo ``/handoff`` SKILL.md invokes it rather than
writing the fields itself.

Fourth addition to the DR-212 sanctioned ``handoff.*`` in-place frontmatter-
mutation population (``handoff.transition``, ``handoff.stamp``,
``handoff.normalize`` — see ``ipc.py`` doctrine comment, "HANDOFF lifecycle ops
(4, DR-212)"). Mirrors ``handoff_stamp.py`` for the RMW/containment scaffold
(``locked_rmw``, ``contained_path``, ``main_worktree_root``, ``frontmatter.
primitives``), but — unlike ``handoff.stamp``, which has no post-mutation
validator — mirrors ``handoff_transition.py``'s post-mutation
``validate_frontmatter()`` gate: the mutated frontmatter is validated INSIDE the
``locked_rmw`` closure, and any validation error raises ``MutateAbort`` (file
left byte-unchanged) rather than writing an invalid stamp.

Idempotency (D1, full-target-state convergence — NOT "is handoff_phase
present?"): a no-op fires ONLY when every intended field (handoff_phase, and,
for phase=execution, all four execution_authorized_* fields) already equals its
intended value. A partial prior stamp (e.g. handoff_phase written but the four
fields missing, from a crashed prior run) CONVERGES to the full stamp on
re-run rather than being skipped. A phase re-stamp (e.g. a target already
carrying ``handoff_phase: continuation``, now asked for ``execution``)
overwrites to the requested phase + fields — read-from-plan makes the target
state fully deterministic from (handoff_path, phase, plan_path) alone. The
kind-gate (H-CROSS-EXEC-2, ``kind != "session-handoff"``) is checked before,
and independently of, this convergence check — a kind mismatch always fails
loud, even on an otherwise-already-converged target.

Spec backlink: docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md § C4/D1
DR-212 compliance: docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md § D2

Self-registration: importing this module fires ``@register_op("handoff.stamp_phase",
_handler)`` as a side-effect. Add this module to coordinator_core/ops/__init__.py
to trigger registration at start_server() time.

P9 WORKTREE DERIVATION: ``_OP_KEY_SCOPE`` keys this op ``"common_dir"``, so
``repo_root`` arrives as ``<worktree>/.git``. All state/handoffs/ and
docs/plans/ paths are built from ``main_worktree_root(repo_root)`` — never
from ``repo_root`` directly (which would scan .git/state/, always empty).

Negative-spec (hard-won; DR-212 § 3 D2 five-bound affirmation lives in
coordinator_core/authz/classification.py):
  - Does NOT git-commit. Pure frontmatter file mutation only (D2(iv)).
  - Does NOT touch more than one state/handoffs/*.md file per invocation
    (D2(ii) — single-target-file-only, not the handoff.normalize batch
    exception).
  - Does NOT write any state/handoffs/*.md BODY content — frontmatter fields
    only (D2(iii)).
  - Does NOT accept explicit execution_authorized_* params or a "both" value-
    source mode — plan_path-read-from-plan is the SOLE v1 value-source
    (anti-scope, plan D1). Removes the both-given-disagree branch entirely.
  - Does NOT recompute / git-hash-object execution_authorized_sha — reads it as
    an OPAQUE STRING off the plan frontmatter and copies it verbatim. Content-
    binding verification of the SHA is /pickup's premise-verification job, not
    this op's.
  - Does NOT read plan_path unconstrained — confined to <worktree>/docs/plans/
    via contained_path(), fail loud on escape (mirrors handoff_path's own
    state/handoffs/ containment guard).
  - Reachable only over the ungated in-process command-type surface — never
    HTTP (D2(v); _OP_KEY_SCOPE common_dir, same as the other three handoff.*
    lifecycle ops).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

_LOG = logging.getLogger(__name__)

#: The two sanctioned handoff_phase values (example-doctrine-repo contract; schema enum).
_VALID_PHASES = ("continuation", "execution")

#: The four-field execution-authorization stamp, byte-identical to the
#: pinned convention (plan-execute-session-split.md § Pinned conventions).
_EXEC_FIELDS = (
    "execution_authorized_by",
    "execution_authorized_at",
    "execution_authorized_sha",
    "execution_authorized_note",
)

#: Vendored handoff schema path — relative to this file's package location
#: (mirrors handoff_transition.py's _SCHEMA_PATH).
_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "handoff.schema.json"
)


# ---------------------------------------------------------------------------
# Reply helpers
# ---------------------------------------------------------------------------


def _ok(applied: bool, message: str) -> dict:
    """Return exit_code=0 reply."""
    return {"exit_code": 0, "applied": applied, "message": message}


def _err(message: str) -> dict:
    """Return exit_code=1 reply (error; no write performed)."""
    _LOG.warning("handoff.stamp_phase: %s", message)
    return {"exit_code": 1, "applied": False, "error": message}


# ---------------------------------------------------------------------------
# Post-mutation validation gate (mirrors handoff_transition.py's _validate_fm)
# ---------------------------------------------------------------------------


def _validate_fm(fm_text: str) -> list:
    """Parse fm_text as YAML and validate against the vendored handoff schema.

    Returns a (possibly empty) list of error dicts.  Empty → valid.  Catches
    YAML parse errors and surfaces them as a synthetic error entry, exactly as
    handoff_transition.py's _validate_fm does.
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SCHEMA_PATH)


# ---------------------------------------------------------------------------
# Plan-frontmatter read helper
# ---------------------------------------------------------------------------


def _read_plan_exec_fields(plan_path: Path) -> dict:
    """Read the four execution_authorized_* fields off a plan's frontmatter.

    execution_authorized_sha is read as an OPAQUE STRING FIELD and never
    recomputed / git-hash-object'd (anti-scope, load-bearing — that content-
    binding check is /pickup's premise-verification job). Raises MutateAbort
    (via the caller, translated to exit_code=1) if the plan file cannot be
    parsed or any of the four values is missing/empty.
    """
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _PlanReadError(f"cannot read plan file: {plan_path}: {exc}") from exc

    split = split_frontmatter(text)
    if split is None:
        raise _PlanReadError(f"no valid YAML frontmatter block in plan: {plan_path}")

    values: dict = {}
    missing = []
    # Unquoted read: execution_authorized_at is an ISO timestamp whose colons
    # trip serialize_yaml_scalar's structural-quoting, so the plan may carry it
    # single-quoted. Reading it raw would re-serialize a quoted value on the
    # handoff (escalating '' escaping on every re-stamp) and would never compare
    # equal to the handoff's own value in the D1 convergence check below.
    for field in _EXEC_FIELDS:
        value = read_fm_field_unquoted(split.fm_text, field)
        if value is None or not str(value).strip():
            missing.append(field)
        else:
            values[field] = value
    if missing:
        raise _PlanReadError(
            "phase=execution requires all four execution_authorized_* fields "
            f"present-and-non-empty on the plan frontmatter — missing/empty: "
            f"{missing} on {plan_path}"
        )
    return values


class _PlanReadError(Exception):
    """Raised when the cited plan's frontmatter cannot supply the four-field stamp."""


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("handoff.stamp_phase")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "handoff.stamp_phase" handler.

    Stamps ``handoff_phase: {continuation|execution}`` onto a target
    ``state/handoffs/*.md`` baton, and — when ``phase=execution`` — the four
    ``execution_authorized_{by,at,sha,note}`` fields sourced from the cited
    plan's frontmatter (``plan_path``, sole v1 value-source; D1).

    Params:
        handoff_path (str) — absolute or repo-relative path to the target
                              handoff file. Required. Confined to
                              <worktree>/state/handoffs/.
        phase        (str) — "continuation" or "execution". Required.
        plan_path    (str) — absolute or repo-relative path to the plan whose
                              frontmatter carries the four-field stamp.
                              Required when phase="execution" (unused/ignored
                              when phase="continuation"). Confined to
                              <worktree>/docs/plans/.

    Returns a dict with keys:
        exit_code  (int)  — 0 ok (stamped or already-converged no-op) / 1 error
        applied    (bool) — True if the frontmatter was written; False if
                             already at full target state or on error
        message    (str)  — present on exit_code 0
        error      (str)  — present on exit_code 1

    Pre-write guard (H-CROSS-EXEC-2): fails loud, no write, if the target's
    kind != "session-handoff".

    Post-mutation validate gate: validate_frontmatter() runs on the
    post-mutation frontmatter INSIDE the locked_rmw closure; any validation
    error raises MutateAbort (file left unchanged), mirroring
    handoff.transition — NOT handoff.stamp, which has no such gate.

    Idempotency (D1): full-target-state convergence. See module docstring.

    Negative-spec: does NOT git-commit; single-target-file-only; frontmatter
    only, no body writes; ungated in-process command-type surface only.
    """
    handoff_path_raw: str = (params.get("handoff_path") or "").strip()
    phase: str = (params.get("phase") or "").strip()
    plan_path_raw: str = (params.get("plan_path") or "").strip()

    if not handoff_path_raw:
        return _err("missing required param: handoff_path")
    if phase not in _VALID_PHASES:
        return _err(
            f"missing/invalid required param: phase (got {phase!r}) — "
            f"must be one of {_VALID_PHASES}"
        )
    if phase == "execution" and not plan_path_raw:
        return _err(
            "missing required param: plan_path (required when phase=execution — "
            "sole v1 value-source for the execution_authorized_* stamp)"
        )

    # P9: repo_root is required to derive the worktree root (common_dir scope
    # keying guarantees the command-type invoker always supplies --repo; see
    # ipc.py's _OP_KEY_SCOPE["handoff.stamp_phase"] = "common_dir").
    if repo_root is None:
        return _err(
            "handoff.stamp_phase: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    # Resolve + confine handoff_path to <worktree>/state/handoffs/.
    hp = Path(handoff_path_raw)
    if not hp.is_absolute():
        hp = worktree / hp
    hp = contained_path(hp, [worktree / "state" / "handoffs"])
    if hp is None:
        return _err(f"handoff_path escapes state/handoffs/: {handoff_path_raw!r}")
    if not hp.is_file():
        return _err(f"handoff not found on disk: {handoff_path_raw}")

    # Resolve + confine plan_path to <worktree>/docs/plans/ (execution phase only).
    plan_path: Optional[Path] = None
    if phase == "execution":
        pp = Path(plan_path_raw)
        if not pp.is_absolute():
            pp = worktree / pp
        plan_path = contained_path(pp, [worktree / "docs" / "plans"])
        if plan_path is None:
            return _err(f"plan_path escapes docs/plans/: {plan_path_raw!r}")
        if not plan_path.is_file():
            return _err(f"plan not found on disk: {plan_path_raw}")

    return await asyncio.to_thread(
        _stamp_phase, hp, phase, plan_path, handoff_path_raw, repo_root
    )


# ---------------------------------------------------------------------------
# Blocking RMW body (offloaded via asyncio.to_thread — DR-212 D3 async-loop mandate)
# ---------------------------------------------------------------------------


def _stamp_phase(
    handoff_path: Path,
    phase: str,
    plan_path: Optional[Path],
    handoff_path_raw: str,
    repo_root: Path,
) -> dict:
    """Blocking RMW body — resolves target field values then locks/mutates/writes.

    Reads the plan's four execution_authorized_* fields BEFORE acquiring the
    handoff's file lock (read-only, no lock needed for the plan file) so a
    plan-read failure fails loud without ever touching the handoff file.
    """
    exec_values: dict = {}
    if phase == "execution":
        assert plan_path is not None  # guaranteed by caller when phase=execution
        try:
            exec_values = _read_plan_exec_fields(plan_path)
        except _PlanReadError as exc:
            return _err(str(exc))

    _state: dict = {"applied": False, "message": ""}

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                f"handoff.stamp_phase: no valid YAML frontmatter block in: {handoff_path_raw}"
            )

        # Pre-write guard (H-CROSS-EXEC-2): handoff_phase requires kind==session-handoff.
        # Checked BEFORE the write is attempted, rather than relying on the
        # post-mutation validate gate alone to catch a wrong-kind target.
        kind = read_fm_field(split.fm_text, "kind")
        if kind != "session-handoff":
            raise MutateAbort(
                f"handoff.stamp_phase: target kind is {kind!r}, not 'session-handoff' — "
                f"handoff_phase requires kind==session-handoff (H-CROSS-EXEC-2) — "
                f"{handoff_path_raw}"
            )

        # Idempotency (D1): full-target-state convergence — all intended fields
        # already equal intended values → no-op. Not "is handoff_phase present?".
        current_phase = read_fm_field(split.fm_text, "handoff_phase")
        already_converged = current_phase == phase
        if already_converged and phase == "execution":
            for field, intended in exec_values.items():
                if read_fm_field_unquoted(split.fm_text, field) != intended:
                    already_converged = False
                    break
        if already_converged:
            _state["applied"] = False
            _state["message"] = (
                f"{handoff_path_raw} already handoff_phase:{phase} "
                f"(full target state) — no-op"
            )
            return old_text  # byte-identical → locked_rmw skips the write

        fm = split.fm_text

        # handoff_phase — replace if present (phase re-stamp), insert after
        # 'kind' if absent.
        if read_fm_field(fm, "handoff_phase") is not None:
            fm = replace_fm_field(fm, "handoff_phase", phase)
        else:
            fm = insert_fm_field(fm, "handoff_phase", phase, after_key="kind")

        # Execution-only: write the four-field stamp, anchored in order right
        # after handoff_phase (each insert/replace re-reads fm so the anchor
        # chain stays valid across the loop — mirrors _consume's insert-if-
        # absent re-read discipline in handoff_transition.py).
        if phase == "execution":
            anchor = "handoff_phase"
            for field in _EXEC_FIELDS:
                # Defense-in-depth: exec_values is provably complete here today
                # (_read_plan_exec_fields raises _PlanReadError on any missing
                # field, ~260 lines away — see module docstring), but a bare
                # exec_values[field] KeyError would escape this closure's own
                # except (neither MutateAbort/LockTimeout/OSError). Guard
                # locally so a future refactor of that invariant fails loud
                # here too, not with an unhandled exception.
                value = exec_values.get(field)
                if value is None:
                    raise MutateAbort(
                        f"handoff.stamp_phase: exec_values missing field "
                        f"{field!r} — _read_plan_exec_fields is expected to "
                        f"guarantee all _EXEC_FIELDS present-and-non-empty "
                        f"before _mutate is entered; {handoff_path_raw}"
                    )
                if read_fm_field(fm, field) is not None:
                    fm = replace_fm_field(fm, field, value)
                else:
                    fm = insert_fm_field(fm, field, value, after_key=anchor)
                anchor = field

        # Post-mutation schema validation gate — raise MutateAbort to skip the
        # write (mirrors handoff.transition; handoff.stamp has no such gate).
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(
                f"handoff.stamp_phase: post-mutation validation failed: {details}"
            )

        _state["applied"] = True
        _state["message"] = f"stamped handoff_phase:{phase} into {handoff_path_raw}"
        return rebuild(split, fm)

    try:
        locked_rmw(handoff_path, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"cannot read/write handoff file: {exc}")

    return _ok(_state["applied"], _state["message"])
