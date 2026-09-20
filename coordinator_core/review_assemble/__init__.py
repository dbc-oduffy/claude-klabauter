"""
coordinator_core.review_assemble — assemblers for the `/review` skill's
write and read-only compute surfaces.

Members:
  - `exec_auth_stamp` — the atomic frontmatter-mutation op that collapses
    `/review`'s ordinal-narrated execution-authorization stamp sequence
    into one named op.
  - `residue` — the read-only `brief(artifact_arg)` compute that resolves
    plan/diff surface and assembles the applicable residue segments into a
    decision-object envelope (the first member of this package to emit
    one, per `coordinator_core.contract.decision_object`).

This module's own `brief(slice_id=, scope=)` (C5,
docs/plans/2026-09-11-document-scaffolding-is-emitted-not-remembered.md)
computes the `review-findings` scaffold directive through the shared
`coordinator-doc-new` constructor (`roadmap_planning_assemble.
scaffold_directive.build_scaffold_directive`, C1) from THIS ceremony's own
resolved dispatch state — the `--slice`/`--scope` pair a review fan-out
dispatcher has already assigned to one persona, never free text the EM
types. Additive and gated, same shape as C3's `roadmap_planning_assemble.
brief`: a caller supplying neither field gets no directive, so every
pre-C5 caller (including `residue.brief`, which this module's own `brief`
does not wrap) is unaffected. See `_review_findings_directive`'s docstring
for the `--out` computation and negative-spec for what this does NOT do.

This package also exposes a module-level `main(argv)` — the entrypoint the
`coordinator/bin/review-assemble` trampoline calls into. It is a
hand-rolled positional-argv dispatcher (mirrors `pickup_assemble`'s and
`baton_assemble`'s own top-level dispatch shape) over the subcommands this
package's members implement; `brief` is the FALLTHROUGH subcommand — a
bare invocation with no recognized subcommand token is treated as `brief`.
The dispatcher prints the resulting decision object as a BARE JSON object
on stdout (no wrapper envelope) and nothing else on stdout; diagnostics go
to stderr. Exit codes are locally scoped to this CLI: 0 OK, 1 business
(e.g. `residue.ResidueAssembleError`), 2 usage, 3 transport (e.g.
`ResolveCoordinatorCloneError`) — see `main`'s own docstring. When
`--slice`/`--scope` are both supplied on the CLI, `_dispatch_brief` merges
this module's own `brief(...)["directives"]` into the printed decision
object's `directives[]` — `residue.brief`'s own `directives=[]` field is
untouched by C5 (writes stay inside `__init__.py`, never `residue.py`).

Spec backlink: DoE-claude:pln-computed-skills-b8-review-ci-c-ffa5ad, chunk C6
Spec backlink: DoE-claude:pln-review-skill-computed-residue--db84bf, chunk C3
Spec backlink: DoE-claude:pln-review-skill-computed-residue--db84bf, chunk C4
Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-remembered.md, chunk C5

Negative spec (C5's slice): does NOT decide whether a `review-findings`
type is emitted at all — that is `coordinator_core.ops.doctype_hosts`'s
(C0) table, read by C8's coverage pin, never re-derived here. Does NOT
call `coordinator-doc-new` or any other CLI — a compute-half constructor
only (§ Which discriminator this plan uses). Does NOT gate on
`residue.brief`'s own surface resolution — `--slice`/`--scope` are an
independent, caller-resolved pair the review fan-out dispatcher supplies,
orthogonal to plan/diff/roadmap surface inference. Does NOT touch
`residue.py` — this file's own writes are the entirety of C5's footprint.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError
from coordinator_core.review_assemble import residue
from coordinator_core.roadmap_planning_assemble.scaffold_directive import (
    Flag,
    build_scaffold_directive,
)
from coordinator_core.session.machinery_paths import SHARE_RELDIR


class _ReviewAssembleExitCode:
    """Exit codes locally scoped to the `review-assemble` CLI surface.

    The line is drawn at the business/transport boundary, per the CLI's own
    contract: content-root-unresolvable is TRANSPORT, zero-applicable-segments
    is BUSINESS. The two are deliberately not conflated."""

    SUCCESS = 0
    BUSINESS = 1
    USAGE = 2
    TRANSPORT = 3


#: The printed usage string's `--surface` value set, derived from
#: `residue.EXPLICIT_SURFACES` (the caller-facing surface vocabulary) rather
#: than a fourth hand-spelled copy -- three hand-synced literals is how the
#: help text went stale after C2 added `roadmap` (Review: code-reviewer --
#: C2 residual).
_SURFACE_USAGE = "|".join(residue.EXPLICIT_SURFACES)


# C5: the shared constructor's (C1) per-type required-flag computation for
# this host's one emitted row (coordinator_core/ops/doctype_hosts.py --
# keyed (type="review-findings", ceremony="review-assemble"),
# module=this package). `--scope` is comma-joined at the call site (the
# real parser's `--scope` is a single `PATH[,PATH...]` string argument, not
# an `action="append"` flag) -- same reason `roadmap_planning_assemble`
# joins `--goals` rather than letting the shared constructor's per-item
# repeat shape run over a list value.
_REVIEW_FINDINGS_FLAG_SPEC: tuple[Flag, ...] = (
    Flag("--slice", "slice_id", required=True),
    Flag("--scope", "scope", required=True),
)


def _review_findings_out_slug(scope: Sequence[str]) -> str:
    """Lowercase-dash slug of the joined scope paths, mirroring
    `coordinator-doc-new`'s own `_slug_from_scope` closely enough for a
    computed (never free-text) `--out` default -- collapses any run of
    non-alphanumeric characters to a single dash and strips leading/
    trailing dashes."""
    text = ",".join(scope)
    out = []
    prev_dash = False
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "scope"


def _review_findings_directive(
    slice_id: str, scope: Sequence[str], *, session_id: Optional[str] = None
) -> dict[str, Any]:
    """Computes the `review-findings` scaffold directive through the
    shared constructor (C1), from a review fan-out dispatcher's own already
    -resolved `slice_id`/`scope` assignment -- never a caller-supplied
    free-text argument (AC3).

    `--out` is computed here (never left to `coordinator-doc-new`'s own
    session-scoped default) under
    `coordinator_core.session.machinery_paths.SHARE_RELDIR` -- the DR-091
    one-home every typed subagent sidecar reader resolves -- keyed by
    `session_id` (an explicit override, else the first of
    `CLAUDE_SESSION_ID`/`CLAUDE_CODE_SESSION_ID` that resolves in the
    process environment, else the literal `"unknown-session"`; this is
    resolved PROCESS state, never a value the caller types), today's date,
    `slice_id` and a slug of `scope`. `--out` containment (AC4) and the
    `already_satisfied` existence predicate are the shared constructor's
    own job, not re-implemented here.
    """
    root = Path.cwd()
    today = date.today().isoformat()
    resolved_session_id = (
        session_id
        or os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or "unknown-session"
    )
    out_path = os.path.join(
        *SHARE_RELDIR.split("/"),
        resolved_session_id,
        f"{today}-codereview-slice{slice_id}-{_review_findings_out_slug(scope)}.md",
    )
    resolved = {
        "slice_id": slice_id,
        "scope": ",".join(scope),
        "out": out_path,
    }
    return build_scaffold_directive(
        "d-scaffold-review-findings",
        "review-findings",
        resolved,
        _REVIEW_FINDINGS_FLAG_SPEC,
        root=root,
    )


def brief(
    *,
    slice_id: Optional[str] = None,
    scope: Optional[Sequence[str]] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """C5: this module's own scaffold-emission compute -- distinct from
    `residue.brief` (the resident-segment renderer this module does not
    wrap). Returns `{"directives": [...]}`: a `review-findings` directive
    when the caller has resolved BOTH `slice_id` and `scope` (the review
    fan-out dispatcher's own per-persona assignment), else an empty list --
    additive and gated, same shape as `roadmap_planning_assemble.brief`'s
    C3 precedent, so a caller supplying neither is unaffected.
    """
    directives: list[dict[str, Any]] = []
    if slice_id and scope:
        directives.append(
            _review_findings_directive(slice_id, scope, session_id=session_id)
        )
    return {"directives": directives}


def _usage() -> int:
    print(
        "usage: review-assemble [brief] [--artifact <path>] "
        f"[--surface {_SURFACE_USAGE}] [--slice <id> --scope <comma-paths>]",
        file=sys.stderr,
    )
    return _ReviewAssembleExitCode.USAGE


def _dispatch_brief(rest: list[str]) -> int:
    """`brief [--artifact <path>] [--surface plan|diff|roadmap] [--slice <id>
    --scope <comma-paths>]` — read-only, prints the bare decision object as
    JSON on stdout. `--surface`, when given, is an already-resolved surface
    from the caller/engine (per `coordinator/skills/review/SKILL.md`) and
    takes HIGHEST precedence over inference — see `residue.brief`'s
    `explicit_surface` param and the module's `--surface RESOLUTION
    CONTRACT` docstring. See module docstring for the exit-code mapping
    this applies to `residue.brief`'s exception types.

    `--slice`/`--scope` (C5) are this module's OWN `brief`'s inputs, never
    `residue.brief`'s — when both are supplied, this module's own
    `review-findings` scaffold directive is merged into the printed
    decision object's `directives[]`; `residue.brief` itself always runs
    unchanged and its own `directives=[]` field is never mutated in place.
    """
    artifact_arg: Optional[str] = None
    explicit_surface: Optional[str] = None
    slice_id: Optional[str] = None
    scope_arg: Optional[str] = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--artifact":
            if i + 1 >= len(rest):
                return _usage()
            artifact_arg = rest[i + 1]
            i += 2
        elif tok == "--surface":
            if i + 1 >= len(rest):
                return _usage()
            explicit_surface = rest[i + 1]
            i += 2
        elif tok == "--slice":
            if i + 1 >= len(rest):
                return _usage()
            slice_id = rest[i + 1]
            i += 2
        elif tok == "--scope":
            if i + 1 >= len(rest):
                return _usage()
            scope_arg = rest[i + 1]
            i += 2
        else:
            print(f"review-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage()

    try:
        decision_object = residue.brief(artifact_arg, explicit_surface=explicit_surface)
    except ResolveCoordinatorCloneError as exc:
        print(str(exc), file=sys.stderr)
        return _ReviewAssembleExitCode.TRANSPORT
    except residue.ResidueUsageError as exc:
        print(str(exc), file=sys.stderr)
        return _ReviewAssembleExitCode.USAGE
    except residue.ResidueAssembleError as exc:
        print(str(exc), file=sys.stderr)
        return _ReviewAssembleExitCode.BUSINESS

    if slice_id and scope_arg:
        scaffold = brief(slice_id=slice_id, scope=scope_arg.split(","))
        decision_object = dict(decision_object)
        decision_object["directives"] = [
            *decision_object.get("directives", []),
            *scaffold["directives"],
        ]

    print(json.dumps(decision_object, indent=2, sort_keys=True))
    return _ReviewAssembleExitCode.SUCCESS


#: Known subcommand tokens -> handler. `brief` is also reachable via
#: FALLTHROUGH (see `main`) so it does not strictly need to appear here,
#: but registering it keeps this the one place a new subcommand is added.
_SUBCOMMANDS = {
    "brief": _dispatch_brief,
}


def main(argv: list[str]) -> int:
    """`review-assemble` CLI entrypoint — hand-rolled positional argv
    dispatch over `_SUBCOMMANDS`.

    `brief` is the FALLTHROUGH subcommand: when `argv` is empty, or its
    first token is not a recognized subcommand name (e.g. it opens with a
    flag like `--artifact`), the entire `argv` is passed to `_dispatch_brief`
    unchanged — a bare `review-assemble` invocation briefs. This mirrors
    `orient_assemble`'s cadence-implicit-default shape: the common case
    needs no subcommand word at all.
    """
    if argv and argv[0] in ("--help", "-h"):
        print(
            "usage: review-assemble [brief] [--artifact <path>] "
            f"[--surface {_SURFACE_USAGE}] [--slice <id> --scope <comma-paths>]"
        )
        return _ReviewAssembleExitCode.SUCCESS
    if argv and argv[0] in _SUBCOMMANDS:
        return _SUBCOMMANDS[argv[0]](argv[1:])
    return _dispatch_brief(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
