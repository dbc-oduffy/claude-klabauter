"""
coordinator_core.ops.sizing_ship — JSON-RPC "sizing.ship" operation.

Purpose: the `shipped` sibling of `sizing_decline.py` — the single addressable
applier for a sizing-object's `shipped` terminal status when the work shipped
WITHOUT a plan ever being minted for it. PM ruling (2026-08-13, verbatim):
"sizings shouldn't always need plans, it should mint closed with
workstream-complete. bookkeeping for bookkeeping's sake is a bit of a bore."

Why a standalone op, not folded into either existing terminal-status applier
(mirroring `sizing_decline.py`'s own "why a standalone op" analysis, restated
for the shipped case):
  - `deliverable.cascade_terminal`'s sizing write side (`_advance_one_sizing`
    in `ops/deliverable_cascade.py`) already writes `status: shipped` in
    place — but it fires FROM a plan reaching `status: implemented`
    (`plan_status_transition.main()`). A `spec-dispatch`- or `plan`-routed
    sizing whose session ships the work WITHOUT minting a plan never produces
    that `status: implemented` transition, so the cascade never fires — there
    is no plan to trigger it, and (per that op's own docstring) no minting-a-
    plan step this ruling now says should not always be required.
  - quick-wrap's dispatch-routed closure step writes `status: shipped`
    directly, but only for `dispatch`-routed sizings whose session closes
    through quick-wrap. A `spec-dispatch`/`plan`-routed sizing that ships
    without ever entering quick-wrap's dispatch-closure path (the exact gap
    the stranded live example — `state/sizings/2026-08-13-chase-down-the-
    frontmatter-drift-failure.yaml`, `route: spec-dispatch`, no plan FK,
    work fully shipped, `status: sized` — demonstrates) has neither writer
    reach it.
  - Both existing writers already exist and are correct for the cases they
    cover; only a non-plan, non-dispatch-closure trigger is missing. This op
    IS that trigger's applier — the writer `deliverable.cascade_terminal`
    already has (`status`/`{"shipped"}` in place) with a different, narrower
    single-target entrypoint that does not require a plan to exist.

Semantic decisions (the judgment calls, each justified):
  - Legal predecessor statuses: `{"sized", "routed"}`. A session can dispatch
    off a sizing before it is ever routed (`sized`) or after routing
    (`routed`) and ship the work in either case without a plan ever being
    minted — the stranded live example is itself `status: sized`. Both are
    therefore legal predecessors, unlike `sizing_decline` (which accepts only
    `routed`, because "declined" specifically means "routed, then the spend
    was refused" — see that op's own docstring for why `sized` does not
    qualify there).
  - `draft` is NOT a legal predecessor. A `draft` sizing has no route chosen
    yet (`_SIZING_LIVE_STATUS`'s own comment in deliverable_cascade.py: "draft
    has no route chosen yet (nothing downstream to be consistent WITH)") — no
    work has been dispatched off it, so there is nothing that could have
    shipped. Writing `status: shipped` over a `draft` sizing would assert a
    fact the record's own history cannot support.
  - Idempotent no-op when already `shipped`: exit_code 0, applied False,
    byte-identical no-op — exactly `sizing_decline`'s own idempotency-floor
    shape, and the same floor `_advance_one_sizing` gives the cascade's own
    write.
  - Fail loud on `declined` or `superseded`: both are different, incompatible
    terminal/near-terminal facts (`declined` = "routed, then the spend was
    refused"; `superseded` = "replaced by a later sizing for the same
    intent") — this op must never overwrite either with "shipped", which
    would erase the more specific true fact in favor of a different one.
  - Writes exactly one field, `status`. Never `pm_resolution`, `fork`,
    `xl_exit`, `plan`, or anything else — the schema is
    `additionalProperties: false`, and (unlike `deliverable.cascade_terminal`'s
    sizing write side, which also writes the `plan` FK because it fires FROM
    a plan) this op has no plan to record: the entire point of it existing is
    that no plan was ever minted.

Caller seam (report-only, not wired in this dispatch — see docstring of the
dispatching brief / run-report sidecar for the seam found and why it is not
wired here): a workstream-complete ceremony body reaching a terminal-positive
outcome for a `spec-dispatch`/`plan`-routed sizing with no plan FK is this
op's intended trigger. The ceremony body itself lives in coordinator-claude and needs
a cross-repo memo — out of scope for this engine-primitive dispatch.

Scope, deliberately narrow (same "one value, its applier, its consumers, one
migration" discipline `sizing_decline.py` names):
  - Writes exactly one field: `status: shipped`.
  - Does NOT resolve or write a `plan` FK — there is none to record; that is
    the entire premise of this op's existence.
  - Does NOT touch `pm_resolution`, `surfaced_to_pm`, `fork`, `xl_exit`, or
    any other field.
  - Does NOT cascade, does NOT fan out to any other artifact, does NOT
    git-commit. Pure single-file frontmatter-shaped (whole-document YAML)
    mutation, same `locked_rmw` RMW discipline as every other mutating op in
    this package.
  - Does NOT accept `draft`/`declined`/`superseded`/`shipped` as the
    pre-transition state — only `sized`/`routed` (or `shipped` itself, for
    the idempotent no-op).
  - Does NOT require a `decision_record` param — unlike a decline (a
    direction-class refusal that per the schema's own description must
    "always [be] paired with a decision-record backlink recording why"), a
    ship is not a PM-directed refusal decision; the schema's `shipped`
    description carries no equivalent backlink requirement, and this op does
    not invent one it was not asked for.

Negative-spec:
  - Does NOT resolve or guess `shipped_in`/ship-commit evidence — the
    sizing-object schema has no such field (see `_advance_one_sizing`'s own
    docstring: "Deliberately does NOT write `shipped_in`... those are
    handoff-shaped ship-commit provenance").
  - Does NOT write or touch a `plan` FK.
  - Does NOT cascade to any other artifact kind.
  - Does NOT wire any `workstream_complete/` ceremony caller — that wiring is
    explicitly out of scope for this dispatch (see caller-seam note above).

Spec backlink: PM ruling 2026-08-13 (verbatim in module docstring above);
debt entry state/debt-backlog/2026-08-13-a-spec-dispatch-sizing-that-never-
mints-5393ec7d7f83.yaml.
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
# established per-module convention (see e.g. deliverable_cascade._SIZING_SCHEMA_PATH,
# sizing_decline._SIZING_SCHEMA_PATH).
_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)

#: Only a `sized` or `routed` sizing can transition to `shipped` via this op —
#: see module docstring for why `draft` is excluded and both `sized`/`routed`
#: are included (unlike `sizing_decline`'s narrower `{"routed"}`).
_SHIPPABLE_FROM = frozenset({"sized", "routed"})

#: Terminal/near-terminal statuses this op must never overwrite — each names a
#: different, incompatible fact from "shipped" (see module docstring).
_INCOMPATIBLE_TERMINAL = frozenset({"declined", "superseded"})


def _validate_sizing_fm(fm_text: str) -> list:
    """Parse fm_text as whole-document YAML and validate against the sizing-object
    schema. Mirrors sizing_decline._validate_sizing_fm's contract exactly.
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SIZING_SCHEMA_PATH)


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


@register_op("sizing.ship")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.ship" handler — the applier for the non-plan-non-dispatch
    shipped-sizing gap (see module docstring).

    Plain `def`, deliberately not `async def` — mirrors sizing_decline._handler's
    own rationale exactly: every step here is blocking (path stats, `locked_rmw`),
    so a zero-await `async def` would make DISPATCH_TIMEOUT_SECS unenforceable
    (incident remediated at 3241c7c95573).

    Params:
        sizing_path (str) — absolute or repo-relative path to the sizing-object
                             under `state/sizings/`. Required.

    Returns a dict with keys:
        exit_code  (int)  — 0 ok (write or idempotent no-op) / 1 error.
        applied    (bool) — True iff `status` was written; False on an idempotent
                             no-op (already `shipped`) or on error.
        message    (str)  — present on exit_code 0; human-readable outcome.
        error      (str)  — present on exit_code 1; human-readable reason.

    Exit-code contract:
        exit_code 1 — missing sizing_path; sizing_path escapes state/sizings/ or is
                      not found on disk; current `status` is neither `sized`,
                      `routed`, nor already `shipped`; post-mutation schema
                      validation fails; lock timeout.
        exit_code 0, applied True  — status written sized|routed -> shipped.
        exit_code 0, applied False — status already shipped; idempotent no-op.
    """
    sizing_path_raw: str = (params.get("sizing_path") or "").strip()

    if not sizing_path_raw:
        return _err("missing required param: sizing_path")
    if repo_root is None:
        return _err(
            "sizing.ship: repo_root is required "
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

    _state = {"applied": False, "prior_status": None}

    def mutate(old_text: str) -> str:
        # Whole-document YAML, no `---` frontmatter fence — same shape as
        # sizing_decline._handler's own mutate closure.
        current_status = read_fm_field_unquoted(old_text, "status")
        _state["prior_status"] = current_status

        if current_status == "shipped":
            # Already terminal — idempotency floor, byte-identical no-op.
            return old_text

        if current_status in _INCOMPATIBLE_TERMINAL:
            raise MutateAbort(
                f"refusing to ship {p}: current status is {current_status!r}, a "
                "different, incompatible terminal fact — 'declined' means the spend "
                "was refused, 'superseded' means replaced by a later sizing; neither "
                "is 'shipped' and this op never overwrites one with the other"
            )

        if current_status not in _SHIPPABLE_FROM:
            raise MutateAbort(
                f"refusing to ship {p}: current status is {current_status!r}, not one "
                f"of {sorted(_SHIPPABLE_FROM)} (or already 'shipped') — a 'draft' "
                "sizing has no route chosen yet and nothing could have shipped off it"
            )

        new_text = replace_fm_field(old_text, "status", "shipped")
        errors = _validate_sizing_fm(new_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"ship: post-mutation schema validation failed: {details}")

        _state["applied"] = True
        return new_text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object not found: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "ship: mutation aborted")

    if _state["applied"]:
        return {
            "exit_code": 0,
            "applied": True,
            "message": (
                f"shipped {sizing_path_raw} "
                f"(status: {_state['prior_status']} -> shipped)"
            ),
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": f"{sizing_path_raw} already shipped — idempotent no-op",
    }
