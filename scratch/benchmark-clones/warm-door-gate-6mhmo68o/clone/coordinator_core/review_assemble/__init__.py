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
`ResolveCoordinatorCloneError`) — see `main`'s own docstring.

Spec backlink: DoE-claude:pln-computed-skills-b8-review-ci-c-ffa5ad, chunk C6
Spec backlink: DoE-claude:pln-review-skill-computed-residue--db84bf, chunk C3
Spec backlink: DoE-claude:pln-review-skill-computed-residue--db84bf, chunk C4
"""

from __future__ import annotations

import json
import sys
from typing import Optional

from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError
from coordinator_core.review_assemble import residue


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


def _usage() -> int:
    print(
        f"usage: review-assemble [brief] [--artifact <path>] [--surface {_SURFACE_USAGE}]",
        file=sys.stderr,
    )
    return _ReviewAssembleExitCode.USAGE


def _dispatch_brief(rest: list[str]) -> int:
    """`brief [--artifact <path>] [--surface plan|diff|roadmap]` — read-only, prints
    the bare decision object as JSON on stdout. `--surface`, when given, is
    an already-resolved surface from the caller/engine (per
    `coordinator/skills/review/SKILL.md`) and takes HIGHEST precedence over
    inference — see `residue.brief`'s `explicit_surface` param and the
    module's `--surface RESOLUTION CONTRACT` docstring. See module docstring
    for the exit-code mapping this applies to `residue.brief`'s exception
    types."""
    artifact_arg: Optional[str] = None
    explicit_surface: Optional[str] = None
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
        print(f"usage: review-assemble [brief] [--artifact <path>] [--surface {_SURFACE_USAGE}]")
        return _ReviewAssembleExitCode.SUCCESS
    if argv and argv[0] in _SUBCOMMANDS:
        return _SUBCOMMANDS[argv[0]](argv[1:])
    return _dispatch_brief(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
