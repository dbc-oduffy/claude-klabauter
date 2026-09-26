"""
coordinator_core.ops.ensure_doe_clone — DoE-clone resolution + idempotent
clone-if-absent, ported from the DoE-claude install playbook
(coordinator/commands/install.md)
(the two literal bash fences at lines 731 and 747 of the DoE-claude source).

Purpose: resolve the local DoE-claude clone path (``REPO_DOE_CLAUDE`` env
override, then ``machine-local get repos.doe_claude``) and, if the resolved
directory does not yet contain a ``.git`` (i.e. is not actually cloned),
perform the clone. Emits the exact ``doe_clone: <status>`` contract row the
DoE doc's Phase 7 status table expects on every exit path — this collapses
install.md's own status-row echo/if wrapper into the CLI (M3/D9 pattern,
docs/plans/2026-07-23-skills-carry-no-code-extirpation.md).

Division of labor (unchanged from every other DoE-clone-resolving op in this
slice, e.g. ``gen_doe_root_pointer``): resolution order is env override,
then the ``machine-local`` registry. This module additionally resolves a
clone URL (``REPO_DOE_CLAUDE_URL`` env override, then ``machine-local get
repos.doe_claude_url``) — a widening over the DoE doc block's own literal
text, which read ``DOE_REPO_URL="<operator-supplied or coordinated from
repos.doe_claude_url registry key>"`` (a placeholder comment, not runnable
shell). A real CLI has to resolve an actual URL to invoke ``git clone``, so
this module implements the ``repos.doe_claude_url`` half of that comment
literally and fails loud (a distinct ``doe_clone: failed`` row) when no URL
is resolvable — see docs/plans/2026-07-23-skills-carry-no-code-extirpation.md
port notes for this repo's disposition of the gap.

Negative-spec (deliberately NOT covered here):
  - Does NOT drive the interactive ``AskUserQuestion`` prompt-then-registry-
    set branch install.md documents between its two literal bash fences —
    that branch is a harness-tool (Claude-side ``AskUserQuestion``) concern,
    not something a naked-Python CLI can invoke. install.md retains that
    branch as prose; this module only implements the two runnable fences
    (resolve, and clone-if-absent), matching exactly what a CLI even *can*
    own. When neither env/registry resolves a clone path and this run is
    interactive (not ``--non-interactive``, not ``--check-only``), this
    module reports the same "skipped" disposition as --check-only reports so
    the operator/install.md prose can drive the interactive prompt and
    re-invoke — it does not itself attempt to ask anything.
  - Does NOT validate the `clone_auth` gate (Step Zero in install.md) —
    the doc's own prose says that gate "already validated"; this module
    trusts the same precondition and does not re-check git auth.
"""

from __future__ import annotations

import os
import subprocess
from coordinator_core.win_portability import no_console_passthrough_kwargs
import sys
from typing import List

from coordinator_core import machine_resolver as _machine_resolver
from coordinator_core.session.declared_writes import declare_write

_PROG = "ensure-doe-clone"


def _registry_get(key: str) -> str:
    value = _machine_resolver.registry_get(key)
    return value or ""


def resolve_doe_clone() -> str:
    """Tier 1: REPO_DOE_CLAUDE env. Tier 2: machine-local get repos.doe_claude."""
    env_override = os.environ.get("REPO_DOE_CLAUDE", "")
    if env_override:
        return env_override
    return _registry_get("repos.doe_claude")


def resolve_doe_clone_url() -> str:
    """Tier 1: REPO_DOE_CLAUDE_URL env. Tier 2: machine-local get repos.doe_claude_url."""
    env_override = os.environ.get("REPO_DOE_CLAUDE_URL", "")
    if env_override:
        return env_override
    return _registry_get("repos.doe_claude_url")


def main(argv: List[str]) -> int:
    check_only = "--check-only" in argv
    non_interactive = "--non-interactive" in argv or os.environ.get("COORDINATOR_NON_INTERACTIVE", "").strip() not in (
        "",
        "0",
    )

    doe_clone = resolve_doe_clone()

    if not doe_clone:
        if check_only:
            print("doe_clone: skipped (repos.doe_claude not set)")
            return 0
        if non_interactive:
            msg = (
                "doe_clone: failed (repos.doe_claude not set — pre-seed the registry or set "
                "REPO_DOE_CLAUDE before running --non-interactive install)"
            )
            print(msg, file=sys.stderr)
            print(msg)
            return 1
        print(
            "doe_clone: skipped (repos.doe_claude not set — run the interactive "
            "DoE-clone prompt, then re-invoke)"
        )
        return 1

    doe_clone = doe_clone.rstrip("/")

    has_git = os.path.isdir(os.path.join(doe_clone, ".git"))
    has_coordinator = os.path.isdir(os.path.join(doe_clone, "coordinator"))

    if has_git and has_coordinator:
        print(f"doe_clone: ready ({doe_clone})")
        return 0

    if has_git and not has_coordinator:
        # A git clone of SOMETHING, but not coordinator-claude -- not the
        msg = (
            f"doe_clone: failed ({doe_clone} is a git clone but has no coordinator/ "
            f"-- not coordinator-claude; repoint repos.doe_claude at the correct clone "
            f"or clone the right URL)"
        )
        print(msg, file=sys.stderr)
        print(msg)
        return 1

    if check_only:
        print(f"doe_clone: check failed: {doe_clone} absent (would clone)")
        return 1

    url = resolve_doe_clone_url()
    if not url:
        msg = (
            f"doe_clone: failed (repos.doe_claude_url not resolvable — set REPO_DOE_CLAUDE_URL "
            f"or `machine-local set repos.doe_claude_url <url>` for target {doe_clone})"
        )
        print(msg, file=sys.stderr)
        print(msg)
        return 1

    try:
        rc = subprocess.call(
            ["git", "clone", url, doe_clone],
            **no_console_passthrough_kwargs(),
        )
    except OSError as exc:
        print(f"{_PROG}: git clone raised: {exc}", file=sys.stderr)
        print(f"doe_clone: failed (git clone raised: {exc})")
        return 1

    if rc != 0:
        print(f"doe_clone: failed (git clone exited {rc})")
        return 1

    # a report of what was ACTUALLY written, not of an intended surface.
    declare_write(doe_clone)

    print(f"doe_clone: cloned ({doe_clone})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
