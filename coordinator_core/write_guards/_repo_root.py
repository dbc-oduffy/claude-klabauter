"""coordinator_core.write_guards._repo_root -- shared memoized repo-root
resolver for write guards.

Purpose: ~10 write guards each independently ran their own `git rev-parse
--show-toplevel` subprocess on the same Write/Edit/MultiEdit payload, one
spawn per guard per hook invocation. `engine.py` discovers guard modules by
`pkgutil` walk and invokes each through `guard.check(payload)` -- a single-
positional-dict contract shared by every discovered guard, with no other
engine-to-guard channel. Threading a resolved root through that contract
would mean either adding a payload key (mutating the hook-event dict shape
every guard reads) or changing `check`'s arity across all discovered guards
-- both fleet-visible contract changes this module deliberately avoids.

Instead: this is a plain import-and-call module. It delegates to
`coordinator_core.git.repo_root.show_toplevel`, which already implements a
cwd-keyed, process-lifetime memo (walks for `.git` first, spawns only when
the walk finds nothing) -- see that module's own docstring for the memo
policy. Delegating rather than re-implementing means this module adds ZERO
new subprocess logic and zero new caching logic; it exists only so write
guards have one obvious, guard-package-local import to reach for instead of
each hand-rolling its own `git rev-parse --show-toplevel` call.

AC4 (per-invocation freshness) is a property of the DELEGATE
(`coordinator_core.git.repo_root`), not of this module: one hook process
handles one Write, so the delegate's process-scoped memo already gives every
guard within that one process the same resolution without spawning twice,
while a fresh process (the next hook invocation) gets a fresh resolution.
This module adds no cross-process cache of its own -- see the delegate's own
"Negative caching" section for why a failed resolution is never memoized
either.

Not built here: a cross-process cache. That is a different, larger design
with staleness questions this module does not open (see
docs/plans/2026-08-07-no-window-subprocess-primitive.md, chunk C3).

Spec backlink: pln-no-window-subprocess-primitive-750d2d § C3
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.git.repo_root import show_toplevel as _show_toplevel


def resolve_repo_root(cwd: Optional[str] = None) -> Optional[str]:
    """The enclosing repo's worktree root for `cwd` (defaults to the
    process's current cwd), or `None` if unresolvable. Thin, memoized
    delegate to `coordinator_core.git.repo_root.show_toplevel` -- see module
    docstring.
    """
    return _show_toplevel(cwd=cwd)
