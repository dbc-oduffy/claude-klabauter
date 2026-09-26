"""
coordinator_core.ops.handoff_phase_stamp — JSON-RPC "handoff.stamp_phase" operation.

Purpose: claude-klabauter's generation-stamp mechanism for the DoE-landed ``handoff_phase``
schema contract (DoE ``coordinator/schemas/handoff.schema.json``, plan
``docs/plans/2026-07-17-execution-handoff-phase-doe-contract.md``). Stamps
``handoff_phase: {continuation|execution}`` onto a target ``state/handoffs/*.md``
baton and, when ``phase=execution``, the four-field authorization stamp
(``execution_authorized_{by,at,sha,note}``) — sourced from the cited PLAN's own
frontmatter (``plan_path``, the SOLE v1 value-source; see Decision D1,
``docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md``). Per the
PM's ruling ("claude-klabauter owns the marking, the skill just carries the invocation" —
DR-210 § Decision 1), this op is claude-klabauter's authoritative write path for the
execution-baton overlay. Docstring correction (2026-08-31, cross-repo/archive/
2026-08-20-doe-claude-em-stamp-phase-execution-value-source-is-unsatisfiable.md):
the DoE ``/handoff`` SKILL.md does NOT currently invoke this op — zero
invocations found anywhere in the DoE tree at the time this was reported. This
op is latent: nothing calls it yet, so the value-source question the same
memo raises (this op reads from the cited plan's frontmatter, which is empty
at exactly the point post-DR-174 wiring would stamp ``phase=execution``) is
also latent, not live, and is NOT resolved here.

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

Spec backlink: pln-claude-klabauter-emit-leg-handoff-phase--e1ccf4 § C4/D1
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
    remove_fm_field,
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

_VALID_PHASES = ("continuation", "execution")

_EXEC_FIELDS = (
    "execution_authorized_by",
    "execution_authorized_at",
    "execution_authorized_sha",
    "execution_authorized_note",
)

#: execution handoff verbatim, all-or-none, beside _EXEC_FIELDS — mirrors
#: exec_auth_stamp.RESTAMP_FIELDS byte-for-byte (not imported, to keep this
_RESTAMP_FIELDS = (
    "execution_restamped_by",
    "execution_restamped_at",
    "execution_restamped_from_sha",
    "execution_restamped_note",
)

#: (mirrors handoff_transition.py's _SCHEMA_PATH).
_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "handoff.schema.json"
)


def _ok(applied: bool, message: str) -> dict:
    return {"exit_code": 0, "applied": applied, "message": message}


def _err(message: str) -> dict:
    _LOG.warning("handoff.stamp_phase: %s", message)
    return {"exit_code": 1, "applied": False, "error": message}


def _validate_fm(fm_text: str) -> list:
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SCHEMA_PATH)


def _read_plan_exec_fields(plan_path: Path) -> tuple[dict, dict]:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _PlanReadError(f"cannot read plan file: {plan_path}: {exc}") from exc

    split = split_frontmatter(text)
    if split is None:
        raise _PlanReadError(f"no valid YAML frontmatter block in plan: {plan_path}")

    values: dict = {}
    missing = []
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

    restamp_values: dict = {}
    for field in _RESTAMP_FIELDS:
        value = read_fm_field_unquoted(split.fm_text, field)
        if value is not None and str(value).strip():
            restamp_values[field] = value

    return values, restamp_values


class _PlanReadError(Exception):
    pass


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

    # ipc.py's _OP_KEY_SCOPE["handoff.stamp_phase"] = "common_dir").
    if repo_root is None:
        return _err(
            "handoff.stamp_phase: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    hp = Path(handoff_path_raw)
    if not hp.is_absolute():
        hp = worktree / hp
    hp = contained_path(hp, [worktree / "state" / "handoffs"])
    if hp is None:
        return _err(f"handoff_path escapes state/handoffs/: {handoff_path_raw!r}")
    if not hp.is_file():
        return _err(f"handoff not found on disk: {handoff_path_raw}")

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


def _stamp_phase(
    handoff_path: Path,
    phase: str,
    plan_path: Optional[Path],
    handoff_path_raw: str,
    repo_root: Path,
) -> dict:
    exec_values: dict = {}
    restamp_values: dict = {}
    if phase == "execution":
        assert plan_path is not None
        try:
            exec_values, restamp_values = _read_plan_exec_fields(plan_path)
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
        kind = read_fm_field(split.fm_text, "kind")
        if kind != "session-handoff":
            raise MutateAbort(
                f"handoff.stamp_phase: target kind is {kind!r}, not 'session-handoff' — "
                f"handoff_phase requires kind==session-handoff (H-CROSS-EXEC-2) — "
                f"{handoff_path_raw}"
            )

        current_phase = read_fm_field(split.fm_text, "handoff_phase")
        already_converged = current_phase == phase
        if already_converged and phase == "execution":
            for field, intended in exec_values.items():
                if read_fm_field_unquoted(split.fm_text, field) != intended:
                    already_converged = False
                    break
        if already_converged and phase == "execution":
            if restamp_values:
                for field, intended in restamp_values.items():
                    if read_fm_field_unquoted(split.fm_text, field) != intended:
                        already_converged = False
                        break
            else:
                for field in _RESTAMP_FIELDS:
                    if read_fm_field(split.fm_text, field) is not None:
                        already_converged = False
                        break
        if already_converged:
            _state["applied"] = False
            _state["message"] = (
                f"{handoff_path_raw} already handoff_phase:{phase} "
                f"(full target state) — no-op"
            )
            return old_text

        fm = split.fm_text

        if read_fm_field(fm, "handoff_phase") is not None:
            fm = replace_fm_field(fm, "handoff_phase", phase)
        else:
            fm = insert_fm_field(fm, "handoff_phase", phase, after_key="kind")

        if phase == "execution":
            anchor = "handoff_phase"
            for field in _EXEC_FIELDS:
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

            if restamp_values:
                for field in _RESTAMP_FIELDS:
                    value = restamp_values.get(field)
                    if value is None:
                        raise MutateAbort(
                            f"handoff.stamp_phase: restamp_values missing field "
                            f"{field!r} — _read_plan_exec_fields is expected to "
                            f"guarantee all _RESTAMP_FIELDS present-and-non-empty "
                            f"together before _mutate is entered; {handoff_path_raw}"
                        )
                    if read_fm_field(fm, field) is not None:
                        fm = replace_fm_field(fm, field, value)
                    else:
                        fm = insert_fm_field(fm, field, value, after_key=anchor)
                    anchor = field
            else:
                for field in _RESTAMP_FIELDS:
                    if read_fm_field(fm, field) is not None:
                        fm = remove_fm_field(fm, field)

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
