"""
coordinator_core.ops.sizing_decline — JSON-RPC "sizing.decline" operation.

Purpose: the single addressable applier for a sizing-object's `declined` terminal
status (2026-08-10, docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md
§ C2, AC3). A sizing-object whose routed work is declined on direction-class grounds
gets `status: declined` written through THIS op, never by an EM opening the YAML file
with Edit — the AC3 requirement this module discharges ("A declined sizing gets its
status by a defined path, not by an EM hand-editing YAML").

Why a standalone op, not folded into either existing terminal-status applier: both of
this repo's existing appliers key on work having SHIPPED, and a decline is the
opposite fact.
  - `deliverable.cascade_terminal`'s sizing write side (`_advance_one_sizing` in
    `ops/deliverable_cascade.py`) fires FROM a plan reaching `status: implemented` —
    it is a fan-out cascade triggered by delivery. A declined sizing never mints a
    plan (spec-dispatch/plan/dispatch never run), so there is no `status: implemented`
    transition to trigger it, and nothing to fan out to.
  - `quick-wrap`'s dispatch-routed closure step fires when dispatched work
    completes. A decline is a REFUSAL of the spend before any dispatch happens — there
    is no completing dispatch for that ceremony to close.
A decline is a single-target, PM-directed action taken at the exact moment the
decision record that resolves it is authored (this repo has no automated
decision-record-authoring ceremony to hook into — DR-284, the first instance, was
hand-authored markdown, confirmed by grep: no `coordinator-doc-new`-equivalent op
exists in this repo's own `coordinator_core.ops`, only the vendored
`docgen/dr_allocator.py` id-allocator, which mints a number and writes nothing else).
So this op is the applier itself, called directly by the EM authoring the decision
record — not folded into a session-close ceremony that has no reason to fire for a
decline, per the plan's own C2 body steer.

Scope, deliberately narrow (Anti-scope: "one value, its applier, its consumers, one
migration" — no general sizing-lifecycle refactor):
  - Writes exactly one field: `status: declined`. Never touches `pm_resolution`,
    `superseded_note`, `plan`, or any other field — the PM's own decline reasoning is
    the EM's to write by hand into `pm_resolution` (as DR-284's own migration does),
    not this op's to compose or guess.
  - Only a `routed` sizing can be declined — "routed, then the spend was declined" is
    the enum value's own stated meaning (see the schema description). A `draft` or
    `sized` sizing with no route taken has nothing to decline; a `superseded` or
    `shipped` sizing is already terminal by a different, incompatible fact.
  - Idempotent: a sizing already `declined` is a byte-identical no-op (exit_code 0,
    applied=False), the same idempotency-floor shape every other terminal-status
    write in this package gives (`_advance_one`/`_advance_one_sizing` in
    `deliverable_cascade.py`).
  - `decision_record` is a REQUIRED param — the schema's own `declined` description
    states "Always paired with a decision-record backlink recording why," and this op
    is what makes that true rather than an aspiration: it must resolve to a real file
    on disk under `docs/decisions/` before any write happens (fail loud, no write, on
    a missing/unresolvable pointer). The op does NOT write this pointer into the
    sizing-object itself (the schema has no dedicated field for it, and `pm_resolution`
    is free-text EM-authored prose per its own schema description, not a field this op
    should compose on the EM's behalf) — the requirement is a live-evidence gate, not a
    persisted field, mirroring `premise.evidence`'s own house pattern of demanding an
    executed check without any promise the check's *output* is retained verbatim.

Negative-spec:
  - Does NOT resolve or guess a decline reason, a DR id, or a `pm_resolution` write —
    the caller supplies `decision_record` and (if it wants one recorded) writes
    `pm_resolution` itself, separately.
  - Does NOT touch `surfaced_to_pm` — that field's own negative spec already governs
    its resolution; this op is not a second writer for it.
  - Does NOT cascade, does NOT fan out to any other artifact, does NOT git-commit.
    Pure single-file frontmatter-shaped (whole-document YAML) mutation, same
    `locked_rmw` RMW discipline as every other mutating op in this package.
  - Does NOT accept `sized`/`draft`/`superseded`/`shipped` as the pre-transition
    state — only `routed` (or `declined` itself, for the idempotent no-op).

Spec backlink: pln-a-terminal-status-for-a-declin-2bb298 § C2
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, replace_fm_field
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

# Vendored sizing-object schema path — own local copy per this package's
# established per-module convention (see e.g. deliverable_cascade._SIZING_SCHEMA_PATH).
_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)

#: Only a `routed` sizing can transition to `declined` — see module docstring.
_DECLINABLE_FROM = frozenset({"routed"})


def _validate_sizing_fm(fm_text: str) -> list:
    """Parse fm_text as whole-document YAML and validate against the sizing-object
    schema. Mirrors deliverable_cascade._validate_sizing_fm's contract exactly.
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SIZING_SCHEMA_PATH)


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


@register_op("sizing.decline")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.decline" handler — the applier for AC3.

    Plain `def`, deliberately not `async def`: every step here is blocking
    (path stats, `locked_rmw`), so there is nothing to await. A zero-await
    `async def` op handler is dispatched inside `ipc.dispatch_message`'s
    `asyncio.wait_for` wrapper while never yielding control, which makes
    DISPATCH_TIMEOUT_SECS silently unenforceable for the op (the incident
    remediated at 3241c7c95573). The sync branch keeps that timeout real.

    Params:
        sizing_path      (str) — absolute or repo-relative path to the sizing-object
                                  under `state/sizings/`. Required.
        decision_record   (str) — repo-relative or absolute path to the decision record
                                  (`docs/decisions/*.md`) that resolves this decline.
                                  Required, and must resolve to a real file on disk —
                                  see module docstring's `decision_record` note.

    Returns a dict with keys:
        exit_code  (int)  — 0 ok (write or idempotent no-op) / 1 error.
        applied    (bool) — True iff `status` was written; False on an idempotent
                             no-op (already `declined`) or on error.
        message    (str)  — present on exit_code 0; human-readable outcome.
        error      (str)  — present on exit_code 1; human-readable reason.

    Exit-code contract:
        exit_code 1 — missing sizing_path/decision_record; decision_record does not
                      resolve to a real file; sizing_path escapes state/sizings/ or is
                      not found on disk; current `status` is neither `routed` nor
                      already `declined`; post-mutation schema validation fails; lock
                      timeout.
        exit_code 0, applied True  — status written routed -> declined.
        exit_code 0, applied False — status already declined; idempotent no-op.
    """
    sizing_path_raw: str = (params.get("sizing_path") or "").strip()
    decision_record_raw: str = (params.get("decision_record") or "").strip()

    if not sizing_path_raw:
        return _err("missing required param: sizing_path")
    if not decision_record_raw:
        return _err(
            "missing required param: decision_record — a declined sizing must be "
            "paired with a real decision-record backlink (see the schema's own "
            "`declined` description); this op never declines a sizing on its own say-so"
        )
    if repo_root is None:
        return _err(
            "sizing.decline: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(sizing_path_raw)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "state" / "sizings"]
    p = contained_path(p, allowed_roots)
    if p is None:
        return _err(f"sizing_path escapes state/sizings/: {sizing_path_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_path_raw}")

    dr = Path(decision_record_raw)
    if not dr.is_absolute():
        dr = worktree / dr
    # Review: coordinator-code-reviewer — the decision_record gate must run through
    # contained_path against docs/decisions/, same as sizing_path against
    # state/sizings/ above; an is_file()-only check let any readable file anywhere
    # satisfy the gate.
    dr = contained_path(dr, [worktree / "docs" / "decisions"])
    if dr is None:
        return _err(
            f"decision_record escapes docs/decisions/: {decision_record_raw!r} — "
            "this op requires live evidence the decline is actually recorded, not a "
            "caller's assertion that it will be"
        )
    if not dr.is_file():
        return _err(
            f"decision_record does not resolve to a real file: {decision_record_raw!r} — "
            "this op requires live evidence the decline is actually recorded, not a "
            "caller's assertion that it will be"
        )

    _state = {"applied": False, "prior_status": None}

    def mutate(old_text: str) -> str:
        # Whole-document YAML, no `---` frontmatter fence — same shape as
        # deliverable_cascade._advance_one_sizing's own mutate closure.
        current_status = read_fm_field_unquoted(old_text, "status")
        _state["prior_status"] = current_status

        if current_status == "declined":
            # Already terminal — idempotency floor, byte-identical no-op.
            return old_text

        if current_status not in _DECLINABLE_FROM:
            raise MutateAbort(
                f"refusing to decline {p}: current status is {current_status!r}, "
                f"not one of {sorted(_DECLINABLE_FROM)} (or already 'declined') — "
                "'declined' means 'routed, then the spend was refused'; a sizing "
                "that was never routed has nothing to decline, and 'superseded'/"
                "'shipped' are already terminal by a different, incompatible fact"
            )

        new_text = replace_fm_field(old_text, "status", "declined")
        errors = _validate_sizing_fm(new_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"decline: post-mutation schema validation failed: {details}")

        _state["applied"] = True
        return new_text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object not found: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "decline: mutation aborted")

    if _state["applied"]:
        return {
            "exit_code": 0,
            "applied": True,
            "message": (
                f"declined {sizing_path_raw} (status: routed -> declined), "
                f"per {decision_record_raw}"
            ),
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": f"{sizing_path_raw} already declined — idempotent no-op",
    }
