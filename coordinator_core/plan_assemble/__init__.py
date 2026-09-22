"""
coordinator_core.plan_assemble — assemblers for the `/plan` skill's
read-only compute surface.

Members:
  - `residue` — the read-only `brief(explicit_route)` compute that resolves
    the plan/spec-dispatch route (per `--route RESOLUTION CONTRACT`, ONE
    step, no inference) and assembles the applicable residue segments into
    a decision-object envelope, mirroring
    `coordinator_core.review_assemble.residue`'s emission shape.
  - `predicates` — the wave-2 predicate producers' shared seam
    (`PredicateContext`, `undetermined(...)`). This CLI parses and
    validates `--plan`/`--sizing-object` and the three `caller_flags`
    flags (see `_dispatch_brief`'s docstring) and forwards them to
    `residue.brief`, which builds the `PredicateContext` and assembles the
    envelope's `gates.*` keys.

This package also exposes a module-level `main(argv)` — the entrypoint the
`coordinator/bin/plan-assemble` trampoline calls into. It is a hand-rolled
positional-argv dispatcher (mirrors `review_assemble`'s own top-level
dispatch shape) over the subcommands this package's members implement;
`brief` is the FALLTHROUGH subcommand — a bare invocation with no
recognized subcommand token is treated as `brief`. The dispatcher prints
the resulting decision object as a BARE JSON object on stdout (no wrapper
envelope) and nothing else on stdout; diagnostics go to stderr. Exit codes
are locally scoped to this CLI: 0 OK, 1 business (e.g.
`residue.ResidueAssembleError`), 2 usage (e.g. `residue.RouteUsageError`,
or an explicitly-given `--plan`/`--sizing-object` path that does not
resolve on disk), 3 transport (e.g. `ResolveCoordinatorCloneError`) — see
`main`'s own docstring.

Spec backlink: pln-plan-assemble-brief-route-the-2d016a, chunk C1
Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C1
Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-remembered.md, chunk C4

`brief(...)` (this module, C4): wraps `residue.brief(...)` unchanged, then
appends the `plan` scaffold directive (through the shared
`coordinator_core.roadmap_planning_assemble.scaffold_directive` constructor,
C1) when the resolved route is `"plan"` -- `spec-dispatch` never scaffolds a
`--type plan` document, so `directives` stays `[]` on that arm, byte-
identical to every pre-C4 caller. `residue.py` is untouched (out of this
chunk's writes): the directive is assembled here, over the envelope
`residue.brief` already returns, never inside it.

`--sizing-object`/`--no-sizing-object` (`coordinator-doc-new`'s one
required mutex for `--type plan`) is computed from THIS call's own
`sizing_object_path` -- the same path `_dispatch_brief` already validated
resolves on disk -- never a second free-text argument: present ->
`--sizing-object=<path>`, absent -> `--no-sizing-object`. `--title` is
computed from that same sizing object's own `intent:` field (the PM's ask,
verbatim, already-resolved ceremony state) when a sizing object is
supplied; omitted (never a placeholder minted here) when it is not --
`--title` is optional on the real CLI for `--type plan`, so an absent
value is not a required-field violation (AC3).

Negative-spec: does NOT gate the `plan` directive on `plan_path` (an
ALREADY-authored plan the caller is re-reading residue/gates for) -- it is
gated on `resolved_route == "plan"` alone, mirroring
`roadmap_planning_assemble`'s additive-only, entry-gated shape. Does NOT
parse `--sizing-object`'s YAML for anything but `intent:` -- no route
re-derivation happens here (that is `residue.brief`'s own `_route_from_
sizing_object`, untouched).
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.schema_validate import parse_yaml
from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError
from coordinator_core.plan_assemble import residue
from coordinator_core.roadmap_planning_assemble.scaffold_directive import (
    Flag,
    build_scaffold_directive,
)

# C4: the shared constructor's (C1) per-type required-flag computation for
# this host's one emitted row (coordinator_core/ops/doctype_hosts.py, keyed
# (type="plan", ceremony="plan-assemble"), module=this package). `--title`
# is optional (never `required=True`): it is computed from the cited sizing
# object's `intent:` field when one is supplied, and simply omitted
# otherwise -- `--title` itself is optional on the real CLI for `--type
# plan` (AC3 binds only the flags the CLI itself requires; `--sizing-
# object`/`--no-sizing-object` is that one mutex here, and it is appended
# by hand below -- same store_true-leg workaround
# `roadmap_planning_assemble._roadmap_baton_and_seed_directives` documents,
# never `MutexFlagPair`, which would render the malformed
# `--no-sizing-object=True`).
_PLAN_FLAG_SPEC: tuple[Flag, ...] = (
    Flag("--title", "title", required=False),
)


def _slug(text: str) -> str:
    """Lowercase-dash slug, mirroring `coordinator-doc-new._slug_from_title`'s
    observable shape closely enough for a computed (never free-text)
    `--out` default -- same small helper `roadmap_planning_assemble._slug`
    duplicates rather than importing (one hierarchy per consumer, no
    cross-package private-helper dependency)."""
    out = []
    prev_dash = False
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "untitled"


def _intent_from_sizing_object(path: Path) -> Optional[str]:
    """The `intent:` a sizing object states about itself, or `None` --
    mirrors `residue._route_from_sizing_object`'s best-effort, silent-on-
    failure read (same file, a different key), never a second parser."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = parse_yaml(text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    candidate = parsed.get("intent")
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return None


def _plan_scaffold_directive(sizing_object_path: Optional[Path]) -> dict[str, Any]:
    """C4: emits the `plan` scaffold directive through the shared
    constructor. `--out` is computed the same way
    `coordinator-doc-new._default_output_path` computes it for `--type
    plan` (`docs/plans/<today>-<slug>.md`), never left to the CLI's own
    default, so `already_satisfied` (AC4) can be computed here."""
    root = Path.cwd()
    today = date.today().isoformat()
    intent = (
        _intent_from_sizing_object(sizing_object_path)
        if sizing_object_path is not None
        else None
    )
    slug = _slug(intent) if intent else "untitled"
    resolved: dict[str, Any] = {
        "title": intent,
        "out": f"docs/plans/{today}-{slug}.md",
    }
    directive = build_scaffold_directive(
        "d-scaffold-plan",
        "plan",
        resolved,
        _PLAN_FLAG_SPEC,
        root=root,
    )
    directive["args"].append(
        f"--sizing-object={sizing_object_path}"
        if sizing_object_path is not None
        else "--no-sizing-object"
    )
    return directive


def brief(
    *,
    explicit_route: Optional[str] = None,
    plan_path: Optional[Path] = None,
    sizing_object_path: Optional[Path] = None,
    caller_flags: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """C4: wraps `residue.brief(...)` unchanged, appending the `plan`
    scaffold directive when the resolved route is `"plan"` (see module
    docstring). Every exception `residue.brief` raises propagates
    unchanged."""
    decision_object = residue.brief(
        explicit_route=explicit_route,
        plan_path=plan_path,
        sizing_object_path=sizing_object_path,
        caller_flags=caller_flags,
    )
    resolved_route = decision_object.get("artifact", {}).get("route")
    directives = list(decision_object.get("directives", []))
    if resolved_route == "plan":
        directives.append(_plan_scaffold_directive(sizing_object_path))
    decision_object["directives"] = directives
    return decision_object


class _PlanAssembleExitCode:
    """Exit codes locally scoped to the `plan-assemble` CLI surface.

    The line is drawn at the business/usage/transport boundary, per the
    CLI's own contract: content-root-unresolvable is TRANSPORT,
    zero-applicable-segments is BUSINESS, an out-of-enum `--route` value is
    USAGE. The three are deliberately not conflated."""

    SUCCESS = 0
    BUSINESS = 1
    USAGE = 2
    TRANSPORT = 3


def _usage() -> int:
    print(
        "usage: plan-assemble [brief] [--route plan|spec-dispatch] "
        "[--plan <path>] [--sizing-object <path>] "
        "[--arrival fresh_inbound|return_edge] "
        "[--trampoline true|false] [--collapse-fired-this-pass true|false]",
        file=sys.stderr,
    )
    return _PlanAssembleExitCode.USAGE


#: `caller_flags` bool-valued flags' accepted literal tokens -> Python
#: `bool`. Review: caller-flags fix wires the CLI to the previously-dead
#: `:100`/`:108` rows — an explicit `true`/`false` string, not a bare
#: presence flag, matching `--route`'s "consume the next token as the
#: value" shape rather than inventing a new parsing style for these two.
_BOOL_FLAG_TOKENS = {"true": True, "false": False}

#: `:32a`'s only two legal `caller_flags["arrival"]` values — anything else
#: is a usage error, matching `--route`'s own closed-enum-or-usage-error
#: shape.
_ARRIVAL_VALUES = {"fresh_inbound", "return_edge"}


def _dispatch_brief(rest: list[str]) -> int:
    """`brief [--route plan|spec-dispatch] [--plan <path>]
    [--sizing-object <path>]` — read-only, prints the bare decision object
    as JSON on stdout.

    `--route`, when absent, resolves to the route the supplied
    `--sizing-object` states about itself, and only falls back to
    `residue.DEFAULT_ROUTE` (`"plan"`) when no object narrows it (amended
    2026-08-30). An explicit `--route` still wins over the object, but a
    disagreement is reported on stderr and in `decisions.route_mismatch`
    rather than echoed back as fact — see `residue.brief`'s `explicit_route`
    param and the module's `--route RESOLUTION CONTRACT` docstring.

    `--plan`/`--sizing-object` are both optional and independent of
    `--route`. Per the plan-assemble wave-2 predicates seam
    (`plan_assemble.predicates.PredicateContext`): absent is NOT a usage
    error — it resolves to `None`, and every predicate row keyed on that
    input emits the `undetermined` sentinel downstream. An EXPLICITLY
    supplied path that does not resolve on disk IS a usage error (exit 2)
    — the two failure modes stay distinct, matching the module's existing
    business/usage/transport exit-code contract. This dispatcher parses and
    validates the flags; `residue.brief` builds the `PredicateContext` from
    them and assembles the emitted envelope's `gates.*` keys.

    `--arrival`/`--trampoline`/`--collapse-fired-this-pass` populate
    `PredicateContext.caller_flags["arrival"/"trampoline"/
    "collapse_fired_this_pass"]` — the wire `:32a`/`:100`/`:108` need and
    previously had no CLI flag to source from. All three are optional and
    independent of every other flag; an ABSENT flag keeps its row resolving
    the `undetermined` sentinel (never backfilled to `False`) — this
    dispatcher only forwards what it is explicitly given. `--arrival` takes
    one of `fresh_inbound`/`return_edge` (`:32a`'s only two legal values);
    `--trampoline`/`--collapse-fired-this-pass` take `true`/`false`. Any
    other value for any of the three is a usage error (exit 2), matching
    `--route`'s own closed-enum-or-usage-error shape.

    See module docstring for the exit-code mapping this applies to
    `residue.brief`'s exception types."""
    explicit_route: Optional[str] = None
    plan_path: Optional[Path] = None
    sizing_object_path: Optional[Path] = None
    caller_flags: dict[str, object] = {}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--route":
            if i + 1 >= len(rest):
                return _usage()
            explicit_route = rest[i + 1]
            i += 2
        elif tok == "--plan":
            if i + 1 >= len(rest):
                return _usage()
            candidate = Path(rest[i + 1])
            if not candidate.is_file():
                print(
                    f"plan-assemble: --plan path does not resolve: {rest[i + 1]!r}",
                    file=sys.stderr,
                )
                return _PlanAssembleExitCode.USAGE
            plan_path = candidate
            i += 2
        elif tok == "--sizing-object":
            if i + 1 >= len(rest):
                return _usage()
            candidate = Path(rest[i + 1])
            if not candidate.is_file():
                print(
                    f"plan-assemble: --sizing-object path does not resolve: "
                    f"{rest[i + 1]!r}",
                    file=sys.stderr,
                )
                return _PlanAssembleExitCode.USAGE
            sizing_object_path = candidate
            i += 2
        elif tok == "--arrival":
            if i + 1 >= len(rest):
                return _usage()
            value = rest[i + 1]
            if value not in _ARRIVAL_VALUES:
                print(
                    f"plan-assemble: --arrival must be one of "
                    f"{sorted(_ARRIVAL_VALUES)}, got {value!r}",
                    file=sys.stderr,
                )
                return _PlanAssembleExitCode.USAGE
            caller_flags["arrival"] = value
            i += 2
        elif tok in ("--trampoline", "--collapse-fired-this-pass"):
            if i + 1 >= len(rest):
                return _usage()
            value = rest[i + 1]
            if value not in _BOOL_FLAG_TOKENS:
                print(
                    f"plan-assemble: {tok} must be 'true' or 'false', got {value!r}",
                    file=sys.stderr,
                )
                return _PlanAssembleExitCode.USAGE
            flag_key = (
                "trampoline" if tok == "--trampoline" else "collapse_fired_this_pass"
            )
            caller_flags[flag_key] = _BOOL_FLAG_TOKENS[value]
            i += 2
        else:
            print(f"plan-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage()

    try:
        decision_object = brief(
            explicit_route=explicit_route,
            plan_path=plan_path,
            sizing_object_path=sizing_object_path,
            caller_flags=caller_flags,
        )
    except ResolveCoordinatorCloneError as exc:
        print(str(exc), file=sys.stderr)
        return _PlanAssembleExitCode.TRANSPORT
    except residue.RouteUsageError as exc:
        print(str(exc), file=sys.stderr)
        return _PlanAssembleExitCode.USAGE
    except residue.ResidueAssembleError as exc:
        print(str(exc), file=sys.stderr)
        return _PlanAssembleExitCode.BUSINESS

    print(json.dumps(decision_object, indent=2, sort_keys=True))
    return _PlanAssembleExitCode.SUCCESS


#: Known subcommand tokens -> handler. `brief` is also reachable via
#: FALLTHROUGH (see `main`) so it does not strictly need to appear here,
#: but registering it keeps this the one place a new subcommand is added.
_SUBCOMMANDS = {
    "brief": _dispatch_brief,
}


def main(argv: list[str]) -> int:
    """`plan-assemble` CLI entrypoint — hand-rolled positional argv
    dispatch over `_SUBCOMMANDS`.

    `brief` is the FALLTHROUGH subcommand: when `argv` is empty, or its
    first token is not a recognized subcommand name (e.g. it opens with a
    flag like `--route`), the entire `argv` is passed to `_dispatch_brief`
    unchanged — a bare `plan-assemble` invocation briefs.
    """
    if argv and argv[0] in _SUBCOMMANDS:
        return _SUBCOMMANDS[argv[0]](argv[1:])
    return _dispatch_brief(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
